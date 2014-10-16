from __future__ import print_function

from rest_framework.test import APITestCase
from rest_framework import status
import datetime
import os
from time import sleep

from django.contrib.auth.models import User
from wq.db.contrib.vera.models import ReportStatus, Parameter
from wq.db.contrib.dbio.models import MetaColumn

import unittest

import swapper
Site = swapper.load_model("vera", "Site")
Event = swapper.load_model("vera", "Event")
Report = swapper.load_model("vera", "Report")
EventResult = swapper.load_model("vera", "EventResult")

from django.conf import settings


class DbioTestCase(APITestCase):
    def setUp(self):
        if not settings.SWAP:
            return

        from wq.db.contrib.dbio.tasks import EVENT_KEY
        self.site = Site.objects.find("Site 1")
        self.user = User.objects.create(username='testuser', is_superuser=True)
        self.client.force_authenticate(user=self.user)
        self.valid = ReportStatus.objects.create(
            is_valid=True,
            slug='valid',
            pk=100,
        )

        param1 = Parameter.objects.find('Temperature')
        param1.is_numeric = True
        param1.units = 'C'
        param1.save()

        meta1 = MetaColumn.objects.find('Date')
        meta1.type = 'event'
        meta1.name = 'date'
        meta1.save()

        meta2 = MetaColumn.objects.find('Site')
        meta2.type = 'event'
        meta2.name = 'site'
        meta2.save()

    @unittest.skipUnless(settings.SWAP, "requires swapped models")
    def test_dbio(self):
        """
        Test the full dbio import process, from initial upload thru data import
        """
        # 1. Upload file
        filename = os.path.join(os.path.dirname(__file__), 'testdata.csv')
        with open(filename, 'rb') as f:
            response = self.client.post('/files.json', {'file': f})
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            fileid = response.data['id']

        def url(action):
            return '/files/%s/%s.json' % (fileid, action)

        # 2. Start import process
        response = self.client.get(url('start'))
        self.assertIn('result', response.data)
        self.assertIn('columns', response.data['result'])
        self.assertEqual(len(response.data['result']['columns']), 4)

        # 3. Inspect unmatched columns, noting that
        #    - "site id" is an alias for site
        #    - "notes" is a previously unknown parameter
        post = {}
        for col in response.data['result']['columns']:
            if not col.get('unknown', False):
                continue
            self.assertIn('types', col)
            type_choices = {
                tc['name']: tc['choices'] for tc in col['types']
            }
            self.assertIn("Metadata Column", type_choices)
            self.assertIn("Parameter", type_choices)

            # "Choose" options from dropdown menu choices
            self.assertIn(col['name'], ("notes", "site id"))
            if col['name'] == "notes":
                col_url = "parameters/new"
                type_name = "Parameter"
            elif col['name'] == "site id":
                col_url = "metacolumns/site"
                type_name = "Metadata Column"

            found = False
            for choice in type_choices[type_name]:
                if choice['url'] == col_url:
                    found = True

            self.assertTrue(found, col_url + " not found in choices")
            post["rel_%s" % col['rel_id']] = col_url

        # 4. Post selected options, verify that all columns are now known
        response = self.client.post(url('columns'), post)
        unknown = response.data['result']['unknown_count']
        self.assertFalse(unknown, "%s unknown columns remain" % unknown)

        # 5. Start data import process, wait for completion
        response = self.client.post(url('data'))
        self.assertIn("task_id", response.data)
        task = response.data['task_id']
        done = False
        while not done:
            sleep(1)
            response = self.client.get(url('status'), {'task': task})
            res = response.data
            if res.get('status', None) == "PENDING":
                print("Waiting...")
                continue
            for key in ('status', 'total', 'current', 'skipped'):
                self.assertIn(key, res)
            if res['status'] == "SUCCESS" or res['total'] == res['current']:
                done = True
                self.assertFalse(res['skipped'])

        # 6. Import complete -verify data exists in database
        for event in Event.objects.all():
            self.assertTrue(event.is_valid)
        self.assertEqual(EventResult.objects.count(), 6)
        param = Parameter.objects.find('temperature')
        er = EventResult.objects.get(
            result_type=param, event_date='2014-01-07'
        )
        self.assertEqual(er.result_value_numeric, 1.0)

        param = Parameter.objects.find('notes')
        er = EventResult.objects.get(
            result_type=param, event_date='2014-01-06'
        )
        self.assertEqual(er.result_value_text, "Test Note 2")