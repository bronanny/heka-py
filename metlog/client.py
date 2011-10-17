# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is metlog, with a bit of stealing from pystatsd
# (https://github.com/jsocol/pystatsd).
#
# The Initial Developer of the Original Code is the Mozilla Foundation.
# Portions created by the Initial Developer are Copyright (C) 2011
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   Rob Miller (rmiller@mozilla.com)
#   James Socol (james@mozilla.com)
#
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
#
# ***** END LICENSE BLOCK *****
try:
    import simplejson as json
except ImportError:
    import json
import random
import threading
import time
import zmq

from datetime import datetime
from functools import wraps


class TimerResult(object):
    def __init__(self, ms=None):
        self.ms = ms


class _Timer(object):
    """A contextdecorator for timing."""
    _local = threading.local()

    def __init__(self, client):
        # We have to make sure the client is attached directly to __dict__
        # because the __setattr__ below is so clever. Otherwise the client
        # becomes a thread-local object even though the connection is for the
        # whole process. This error was witnessed under mod_wsgi when using an
        # ImportScript.
        self.__dict__['client'] = client
        random.seed()

    def __delattr__(self, attr):
        """Store thread-local data safely."""
        delattr(self._local, attr)

    def __getattr__(self, attr):
        """Store thread-local data safely."""
        return getattr(self._local, attr)

    def __setattr__(self, attr, value):
        """Store thread-local data safely."""
        setattr(self._local, attr, value)

    def __call__(self, name, timestamp=None, logger=None, severity=None,
                 tags=None, rate=1):
        if callable(name):  # As a decorator, 'name' may be a function.

            @wraps(name)
            def wrapped(*a, **kw):
                with self:
                    return name(*a, **kw)
            return wrapped

        self.name = name
        self.timestamp = timestamp
        self.logger = logger
        self.severity = severity
        self.tags = tags
        self.rate = rate
        return self

    def __enter__(self):
        self.start = time.time()
        self.result = TimerResult()
        return self.result

    def __exit__(self, typ, value, tb):
        dt = time.time() - self.start
        dt = int(round(dt * 1000))  # Convert to ms.
        self.result.ms = dt
        self.client.timing(self, dt)
        del self.start, self.result  # Clean up.
        return False


class MetlogClient(object):
    """
    Client class encapsulating metlog API, and providing storage for default
    values for various metlog call settings.
    """
    _local = threading.local()
    zmq_context = zmq.Context()
    env_version = '0.8'

    def __init__(self, bindstrs, logger='', severity=6):
        self.bindstrs = bindstrs
        self.logger = logger
        self.severity = severity
        self.timer = _Timer(self)

    @property
    def publisher(self):
        if not hasattr(self._local, 'publisher'):
            self._local.publisher = self.zmq_context.socket(zmq.PUB)
            for bindstr in self.bindstrs:
                self._local.publisher.bind(bindstr)
        return self._local.publisher

    def _send_message(self, full_msg):
        json_msg = json.dumps(full_msg)
        self.publisher.send(json_msg)

    def metlog(self, timestamp=None, logger=None, severity=None, payload='',
               tags=None):
        timestamp = timestamp if timestamp is not None else datetime.utcnow()
        logger = logger if logger is not None else self.logger
        severity = severity if severity is not None else self.severity
        tags = tags if tags is not None else dict()
        if hasattr(timestamp, 'isoformat'):
            timestamp = timestamp.isoformat()
        full_msg = dict(timestamp=timestamp, logger=logger, severity=severity,
                        payload=payload, tags=tags,
                        env_version=self.env_version)
        self._send_message(full_msg)

    def timing(self, timer, elapsed):
        if timer.rate < 1 and random.random() >= timer.rate:
            return
        payload = str(elapsed)
        tags = timer.tags if timer.tags is not None else dict()
        tags.update({'type': 'timer', 'name': timer.name, 'rate': timer.rate})
        self.metlog(timer.timestamp, timer.logger, timer.severity, payload,
                    tags)

    def incr(self, name, count=1, timestamp=None, logger=None, severity=None,
             tags=None):
        payload = str(count)
        tags = tags if tags is not None else dict()
        tags.update({'type': 'counter', 'name': name})
        self.metlog(timestamp, logger, severity, payload, tags)