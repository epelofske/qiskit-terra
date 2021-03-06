# -*- coding: utf-8 -*-

# Copyright 2017, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""IbmQ module

This module is used for connecting to the Quantum Experience.
"""
import logging

from marshmallow import ValidationError

from qiskit import QISKitError
from qiskit._util import _camel_case_to_snake_case
from qiskit.backends import BaseBackend, JobStatus
from qiskit.backends.models import BackendStatus

from .api import ApiError
from .ibmqjob import IBMQJob, IBMQJobPreQobj

logger = logging.getLogger(__name__)


class IBMQBackend(BaseBackend):
    """Backend class interfacing with the Quantum Experience remotely.
    """

    def __init__(self, configuration, provider, credentials, api):
        """Initialize remote backend for IBM Quantum Experience.

        Args:
            configuration (dict): configuration of backend.
            provider (IBMQProvider): provider.
            credentials (Credentials): credentials.
            api (IBMQConnector):
                api for communicating with the Quantum Experience.
        """
        super().__init__(provider=provider, configuration=configuration)
        self._api = api
        if self._configuration:
            configuration_edit = {}
            for key, vals in self._configuration.items():
                new_key = _camel_case_to_snake_case(key)
                configuration_edit[new_key] = vals
            self._configuration = configuration_edit
            # FIXME: This is a hack to make sure that the
            # local : False is added to the online device
            self._configuration['local'] = False

        self._credentials = credentials
        self.hub = credentials.hub
        self.group = credentials.group
        self.project = credentials.project

    def run(self, qobj):
        """Run qobj asynchronously.

        Args:
            qobj (dict): description of job

        Returns:
            IBMQJob: an instance derived from BaseJob
        """
        job_class = _job_class_from_backend_support(self)
        job = job_class(self, None, self._api, not self.configuration()['simulator'], qobj=qobj)
        job.submit()
        return job

    def properties(self):
        """Return the online backend properties.

        The return is via QX API call.

        Returns:
            dict: The properties of the backend.

        Raises:
            LookupError: If properties for the backend can't be found.
        """
        properties = self._api.backend_properties(self.name())

        # Convert the properties to snake_case.
        # TODO: ideally should be handled at the API level.
        properties_edit = {}
        for key, val in properties.items():
            new_key = _camel_case_to_snake_case(key)
            properties_edit[new_key] = val

        return properties_edit

    def status(self):
        """Return the online backend status.

        Returns:
            dict: The status of the backend.

        Raises:
            LookupError: If status for the backend can't be found.
            IBMQBackendError: If the status can't be formatted properly.
        """
        api_status = self._api.backend_status(self.name())

        try:
            return BackendStatus.from_dict(api_status)
        except ValidationError as ex:
            raise LookupError(
                "Couldn't get backend status: {0}".format(ex))

    def jobs(self, limit=50, skip=0, status=None, db_filter=None):
        """Attempt to get the jobs submitted to the backend.

        Args:
            limit (int): number of jobs to retrieve
            skip (int): starting index of retrieval
            status (None or JobStatus or str): only get jobs with this status,
                where status is e.g. `JobStatus.RUNNING` or `'RUNNING'`
            db_filter (dict): `loopback-based filter
                <https://loopback.io/doc/en/lb2/Querying-data.html>`_.
                This is an interface to a database ``where`` filter. Some
                examples of its usage are:

                Filter last five jobs with errors::

                   job_list = backend.jobs(limit=5, status=JobStatus.ERROR)

                Filter last five jobs with counts=1024, and counts for
                states ``00`` and ``11`` each exceeding 400::

                  cnts_filter = {'shots': 1024,
                                 'qasms.result.data.counts.00': {'gt': 400},
                                 'qasms.result.data.counts.11': {'gt': 400}}
                  job_list = backend.jobs(limit=5, db_filter=cnts_filter)

                Filter last five jobs from 30 days ago::

                   past_date = datetime.datetime.now() - datetime.timedelta(days=30)
                   date_filter = {'creationDate': {'lt': past_date.isoformat()}}
                   job_list = backend.jobs(limit=5, db_filter=date_filter)

        Returns:
            list(IBMQJob): list of IBMQJob instances

        Raises:
            IBMQBackendValueError: status keyword value unrecognized
        """
        backend_name = self.configuration()['name']
        api_filter = {'backend.name': backend_name}
        if status:
            if isinstance(status, str):
                status = JobStatus[status]
            if status == JobStatus.RUNNING:
                this_filter = {'status': 'RUNNING',
                               'infoQueue': {'exists': False}}
            elif status == JobStatus.QUEUED:
                this_filter = {'status': 'RUNNING',
                               'infoQueue.status': 'PENDING_IN_QUEUE'}
            elif status == JobStatus.CANCELLED:
                this_filter = {'status': 'CANCELLED'}
            elif status == JobStatus.DONE:
                this_filter = {'status': 'COMPLETED'}
            elif status == JobStatus.ERROR:
                this_filter = {'status': {'regexp': '^ERROR'}}
            else:
                raise IBMQBackendValueError('unrecognized value for "status" keyword '
                                            'in job filter')
            api_filter.update(this_filter)
        if db_filter:
            # status takes precedence over db_filter for same keys
            api_filter = {**db_filter, **api_filter}
        job_info_list = self._api.get_status_jobs(limit=limit, skip=skip,
                                                  filter=api_filter)
        job_list = []
        for job_info in job_info_list:
            job_class = _job_class_from_job_response(job_info)
            is_device = not bool(self._configuration.get('simulator'))
            job = job_class(self, job_info.get('id'), self._api, is_device,
                            creation_date=job_info.get('creationDate'),
                            api_status=job_info.get('status'))
            job_list.append(job)
        return job_list

    def retrieve_job(self, job_id):
        """Attempt to get the specified job by job_id

        Args:
            job_id (str): the job id of the job to retrieve

        Returns:
            IBMQJob: class instance

        Raises:
            IBMQBackendError: if retrieval failed
        """
        try:
            job_info = self._api.get_status_job(job_id)
            if 'error' in job_info:
                raise IBMQBackendError('Failed to get job "{}": {}'
                                       .format(job_id, job_info['error']))
        except ApiError as ex:
            raise IBMQBackendError('Failed to get job "{}":{}'
                                   .format(job_id, str(ex)))
        job_class = _job_class_from_job_response(job_info)
        is_device = not bool(self._configuration.get('simulator'))
        job = job_class(self, job_info.get('id'), self._api, is_device,
                        creation_date=job_info.get('creationDate'),
                        api_status=job_info.get('status'))
        return job

    def __repr__(self):
        credentials_info = ''
        if self.hub:
            credentials_info = '{}, {}, {}'.format(self.hub, self.group,
                                                   self.project)
        return "<{}('{}') from IBMQ({})>".format(
            self.__class__.__name__, self.name(), credentials_info)


class IBMQBackendError(QISKitError):
    """IBM Q Backend Errors"""
    pass


class IBMQBackendValueError(IBMQBackendError, ValueError):
    """ Value errors thrown within IBMQBackend """
    pass


def _job_class_from_job_response(job_response):
    is_qobj = job_response.get('kind', None) == 'q-object'
    return IBMQJob if is_qobj else IBMQJobPreQobj


def _job_class_from_backend_support(backend):
    support_qobj = backend.configuration().get('allow_q_object')
    return IBMQJob if support_qobj else IBMQJobPreQobj
