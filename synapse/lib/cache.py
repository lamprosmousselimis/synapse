'''
A few speed optimized (lockless) cache helpers.  Use carefully.
'''
import asyncio
import functools
import collections

import regex

import synapse.exc as s_exc
import synapse.common as s_common

def memoize(size=10000):
    return functools.lru_cache(maxsize=size)

class FixedCache:

    def __init__(self, callback, size=10000):
        self.size = size
        self.callback = callback
        self.iscorocall = asyncio.iscoroutinefunction(self.callback)

        self.cache = {}
        self.fifo = collections.deque()

    def __len__(self):
        return len(self.cache)

    def pop(self, key):
        return self.cache.pop(key, None)

    def put(self, key, val):

        self.cache[key] = val
        self.fifo.append(key)

        while len(self.fifo) > self.size:
            key = self.fifo.popleft()
            self.cache.pop(key, None)

    def get(self, key):
        if self.iscorocall:
            raise s_exc.BadArg('cache was initialized with coroutine.  Must use aget')

        valu = self.cache.get(key, s_common.novalu)
        if valu is not s_common.novalu:
            return valu

        valu = self.callback(key)
        if valu is s_common.novalu:
            return valu

        self.cache[key] = valu
        self.fifo.append(key)

        while len(self.fifo) > self.size:
            key = self.fifo.popleft()
            self.cache.pop(key, None)

        return valu

    async def aget(self, key):
        if not self.iscorocall:
            raise s_exc.BadOperArg('cache was initialized with non coroutine.  Must use get')

        valu = self.cache.get(key, s_common.novalu)
        if valu is not s_common.novalu:
            return valu

        valu = await self.callback(key)
        if valu is s_common.novalu:
            return valu

        self.cache[key] = valu
        self.fifo.append(key)

        while len(self.fifo) > self.size:
            key = self.fifo.popleft()
            self.cache.pop(key, None)

        return valu

    def clear(self):
        self.fifo.clear()
        self.cache.clear()

class LruDict(collections.abc.MutableMapping):
    '''
    Maintains the last n accessed keys
    '''
    def __init__(self, size=10000):
        self.data = collections.OrderedDict()
        self.maxsize = size
        self.disabled = not self.maxsize

    def __getitem__(self, key):
        valu = self.data.__getitem__(key)
        self.data.move_to_end(key)
        return valu

    def get(self, key, default=None):
        '''
        Note:  we override default impl from parent to avoid costly KeyError
        '''
        valu = self.data.get(key, default)
        if key in self.data:
            self.data.move_to_end(key)
        return valu

    def __setitem__(self, key, valu):
        if self.disabled:
            return
        self.data[key] = valu
        self.data.move_to_end(key)
        if len(self.data) > self.maxsize:
            self.data.popitem(last=False)

    def __delitem__(self, key):
        '''
        Ignore attempts to delete keys that may have already been flushed
        '''
        if key in self.data:
            del self.data[key]

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def items(self):
        return self.data.items()

    def values(self):
        return self.data.values()

    def __contains__(self, item):
        '''
        Note:  we override default impl from parent to avoid costly KeyError
        '''
        return item in self.data

# Search for instances of escaped double or single asterisks
# https://regex101.com/r/fOdmF2/1
ReRegex = regex.compile(r'(\\\*\\\*)|(\\\*)')

def regexizeTagGlob(tag):
    '''
    Returns:
        a regular expression string with ** and * interpreted as tag globs

    Precondition:
        tag is a valid tagmatch

    Notes:
        A single asterisk will replace exactly one dot-delimited component of a tag
        A double asterisk will replace one or more of any character.

        The returned string does not contain a starting '^' or trailing '$'.
    '''
    return ReRegex.sub(lambda m: r'([^.]+?)' if m.group(1) is None else r'(.+)', regex.escape(tag))

@memoize()
def getTagGlobRegx(name):
    regq = regexizeTagGlob(name)
    return regex.compile(regq)

class TagGlobs:
    '''
    An object that manages multiple tag globs and values for caching.
    '''
    def __init__(self):
        self.globs = []
        self.cache = FixedCache(self._getGlobMatches)

    def add(self, name, valu, base=None):

        self.cache.clear()

        regx = getTagGlobRegx(name)

        glob = (regx, (name, valu))

        self.globs.append(glob)

        if base:
            def fini():
                try:
                    self.globs.remove(glob)
                except ValueError:
                    pass
                self.cache.clear()
            base.onfini(fini)

    def rem(self, name, valu):
        self.globs = [g for g in self.globs if g[1] != (name, valu)]
        self.cache.clear()

    def get(self, name):
        return self.cache.get(name)

    def _getGlobMatches(self, name):
        return [g[1] for g in self.globs if g[0].fullmatch(name) is not None]
