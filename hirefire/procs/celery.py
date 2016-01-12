from __future__ import absolute_import
from collections import Counter
from itertools import chain

from celery.app import app_or_default

from ..utils import KeyDefaultDict
from . import Proc


class CeleryInspector(KeyDefaultDict):
    """
    A defaultdict that manages the celery inspector cache.
    """

    def __init__(self, app):
        super(CeleryInspector, self).__init__(self.get_status_task_counts)
        self.app = app
        self.route_queues = None

    def get_route_queues(self):
        """Find the queue to each active routing pair.

        Cache to avoid additional calls to inspect().

        Returns a mapping from (exchange, routing_key) to queue_name.
        """
        if self.route_queues is not None:
            return self.route_queues

        worker_queues = self.app.control.inspect().active_queues()
        active_queues = chain.from_iterable(worker_queues.values())

        self.route_queues = {
            (queue['exchange']['name'], queue['routing_key']): queue['name']
            for queue in active_queues
        }
        return self.route_queues

    def get_status_task_counts(self, status):
        """Get the tasks on all queues for the given status.

        This is called lazily to avoid running long methods when not needed.
        """
        if status not in ['active', 'reserved', 'scheduled']:
            raise KeyError('Invalid task status: {}'.format(status))

        route_queues = self.get_route_queues()
        def get_queue(task):
            """Find the queue for a given task."""
            exchange = task['delivery_info']['exchange']
            routing_key = task['delivery_info']['routing_key']
            return route_queues[exchange, routing_key]

        inspected = getattr(self.app.control.inspect(), status)()
        queues = map(get_queue, chain.from_iterable(inspected.values()))
        return Counter(queues)


class CeleryProc(Proc):
    """
    A proc class for the `Celery <http://celeryproject.org>`_ library.

    :param name: the name of the proc (required)
    :param queues: list of queue names to check (required)
    :param app: the Celery app to check for the queues (optional)
    :type name: str
    :type queues: str or list
    :type app: :class:`~celery.Celery`

    Declarative example::

        from celery import Celery
        from hirefire.procs.celery import CeleryProc

        celery = Celery('myproject', broker='amqp://guest@localhost//')

        class WorkerProc(CeleryProc):
            name = 'worker'
            queues = ['celery']
            app = celery

    Or a simpler variant::

        worker_proc = CeleryProc('worker', queues=['celery'], app=celery)

    In case you use one of the non-standard Celery clients (e.g.
    django-celery) you can leave the ``app`` attribute empty because
    Celery will automatically find the correct Celery app::

        from hirefire.procs.celery import CeleryProc

        class WorkerProc(CeleryProc):
            name = 'worker'
            queues = ['celery']

    """
    #: The name of the proc (required).
    name = None

    #: The list of queues to check (required).
    queues = ['celery']

    #: The Celery app to check for the queues (optional).
    app = None

    #: The Celery task status to check for on workers (optional).
    #: Valid options are 'active', 'reserved', and 'scheduled'.
    inspect_statuses = []  # Empty to default to previous results

    def __init__(self, app=None, *args, **kwargs):
        super(CeleryProc, self).__init__(*args, **kwargs)
        if app is not None:
            self.app = app
        self.app = app_or_default(self.app)
        self.connection = self.app.connection()
        self.channel = self.connection.channel()

    def quantity(self, cache=None, **kwargs):
        """
        Returns the aggregated number of tasks of the proc queues.
        """
        if hasattr(self.channel, '_size'):
            # Redis
            return sum(self.channel._size(queue) for queue in self.queues)
        # AMQP
        try:
            from librabbitmq import ChannelError
        except ImportError:
            from amqp.exceptions import ChannelError
        count = 0
        for queue in self.queues:
            try:
                queue = self.channel.queue_declare(queue, passive=True)
            except ChannelError:
                # The requested queue has not been created yet
                pass
            else:
                count += queue.message_count

        if cache is not None and self.inspect_statuses:
            count += self.inspect_count(cache)

        return count

    def inspect_count(self, cache):
        """Use Celery's inspect() methods to see tasks on workers."""
        cache.setdefault('celery_inspect', KeyDefaultDict(CeleryInspector))
        return sum(
            cache['celery_inspect'][self.app][status][queue]
            for status in self.inspect_statuses
            for queue in self.queues
        )
