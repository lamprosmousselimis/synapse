import heapq
import asyncio

import synapse.common as s_common

import synapse.lib.coro as s_coro
import synapse.lib.msgpack as s_msgpack
import synapse.lib.lmdbslab as s_lmdbslab

class SlabSeqn:
    '''
    An append optimized sequence of byte blobs.

    Args:
        lenv (lmdb.Environment): The LMDB Environment.
        name (str): The name of the sequence.
    '''
    def __init__(self, slab: s_lmdbslab.Slab, name: str) -> None:

        self.slab = slab
        self.db = self.slab.initdb(name)

        self.indx = self.nextindx()
        self.offsevents = []  # type: ignore # List[Tuple[int, int, asyncio.Event]] as a heap
        self._waitcounter = 0

    def _wake_waiters(self):
        while self.offsevents and self.offsevents[0][0] < self.indx:
            _, _, evnt = heapq.heappop(self.offsevents)
            evnt.set()

    def add(self, item):
        '''
        Add a single item to the sequence.
        '''
        indx = self.indx
        self.slab.put(s_common.int64en(indx), s_msgpack.en(item), db=self.db)

        self.indx += 1

        self._wake_waiters()

        return indx

    def last(self):

        last = self.slab.last(db=self.db)
        if last is None:
            return None

        lkey, lval = last

        indx = s_common.int64un(lkey)
        return indx, s_msgpack.un(lval)

    def stat(self):
        return self.slab.stat(db=self.db)

    def save(self, items):
        '''
        Save a series of items to a sequence.

        Args:
            items (tuple): The series of items to save into the sequence.

        Returns:
            The index of the first item
        '''
        rows = []
        indx = self.indx

        size = 0
        tick = s_common.now()

        for item in items:

            byts = s_msgpack.en(item)

            size += len(byts)

            lkey = s_common.int64en(indx)
            indx += 1

            rows.append((lkey, byts))

        self.slab.putmulti(rows, append=True, db=self.db)
        took = s_common.now() - tick

        origindx = self.indx
        self.indx = indx

        self._wake_waiters()

        return {'indx': indx, 'size': size, 'count': len(items), 'time': tick, 'took': took, 'orig': origindx}

    def index(self):
        '''
        Return the current index to be used
        '''
        return self.indx

    def nextindx(self):
        '''
        Determine the next insert offset according to storage.

        Returns:
            int: The next insert offset.
        '''
        indx = 0
        with s_lmdbslab.Scan(self.slab, self.db) as curs:
            last_key = curs.last_key()
            if last_key is not None:
                indx = s_common.int64un(last_key) + 1
        return indx

    def iter(self, offs):
        '''
        Iterate over items in a sequence from a given offset.

        Args:
            offs (int): The offset to begin iterating from.

        Yields:
            (indx, valu): The index and valu of the item.
        '''
        startkey = s_common.int64en(offs)

        for lkey, lval in self.slab.scanByRange(startkey, db=self.db):
            indx = s_common.int64un(lkey)
            valu = s_msgpack.un(lval)
            yield indx, valu

    def iterBack(self, offs):
        '''
        Iterate backwards over items in a sequence from a given offset.

        Args:
            offs (int): The offset to begin iterating from.

        Yields:
            (indx, valu): The index and valu of the item.
        '''
        startkey = s_common.int64en(offs)

        for lkey, lval in self.slab.scanByRangeBack(startkey, db=self.db):
            indx = s_common.int64un(lkey)
            valu = s_msgpack.un(lval)
            yield indx, valu

    def rows(self, offs):
        '''
        Iterate over raw indx, bytes tuples from a given offset.
        '''
        lkey = s_common.int64en(offs)
        for lkey, byts in self.slab.scanByRange(lkey, db=self.db):
            indx = s_common.int64un(lkey)
            yield indx, byts

    def get(self, offs):
        '''
        Retrieve a single row by offset
        '''
        lkey = s_common.int64en(offs)
        valu = self.slab.get(lkey, db=self.db)
        return s_msgpack.un(valu)

    def slice(self, offs, size):

        imax = size - 1

        for i, item in enumerate(self.iter(offs)):

            yield item

            if i == imax:
                break

    def sliceBack(self, offs, size):

        imax = size - 1

        for i, item in enumerate(self.iterBack(offs)):

            yield item

            if i == imax:
                break

    def getByIndxByts(self, indxbyts):
        byts = self.slab.get(indxbyts, db=self.db)
        if byts is not None:
            return s_msgpack.un(byts)

    def getOffsetEvent(self, offs):
        '''
        Returns an asyncio Event that will be set when the particular offset is written.  The event will be set if the
        offset has already been reached.
        '''
        evnt = asyncio.Event()

        if offs < self.indx:
            evnt.set()
            return evnt

        # We add a simple counter to the tuple to cause stable (and FIFO) sorting and prevent ties
        heapq.heappush(self.offsevents, (offs, self._waitcounter, evnt))

        self._waitcounter += 1

        return evnt

    async def waitForOffset(self, offs, timeout=None):
        '''
        Returns:
            true if the event got set, False if timed out
        '''

        if offs < self.indx:
            return True

        evnt = self.getOffsetEvent(offs)
        return await s_coro.event_wait(evnt, timeout=timeout)
