from goto import with_goto


class Awaiter:
    def __init__(self):
        self.count = 0

    def __await__(self):
        self.count += 1
        return (yield)


def run_async(func, list=None):
    try:
        func.send(None)
        if list is None:
            while True:
                func.send(None)
        else:
            for item in list:
                func.send(item)
    except StopIteration as e:
        return e.value
    else:
        raise Exception("async did not complete in time")


def test_async():
    @with_goto
    async def func():
        a = Awaiter()
        lst = []
        lst.append(await a)
        goto .x
        lst.append(await a)
        label .x
        lst.append(await a)
        return lst, a.count

    assert run_async(func(), [1, 2, 3]) == ([1, 2], 2)


class AsyncIterator:
    def __init__(self, awaiter):
        self.awaiter = awaiter

    def __aiter__(self):
        return self

    async def __anext__(self):
        val = await self.awaiter
        if val is None:
            raise StopAsyncIteration
        return val


class AsyncIterable:
    def __init__(self, awaiter):
        self.awaiter = awaiter

    def __aiter__(self):
        return AsyncIterator(self.awaiter)


def test_jump_out_of_async_for_and_survive():
    @with_goto
    async def func():
        a = Awaiter()
        lst = []
        for i in range(10):
            async for j in AsyncIterator(a):
                lst.append(j)
                goto .x
            label .x
        return a.count, lst

    async_args = [1, 2, 3, 4, None, 6, 7, 8, 9, None]
    assert run_async(func(), async_args) == (10, [1, 2, 3, 4, 6, 7, 8, 9])


def test_jump_out_of_async_for_and_live():
    @with_goto
    async def func():
        a = Awaiter()
        lst = []
        for i in range(4):
            for j in range(4):
                async for k in AsyncIterator(a):
                    lst.append(k)
                    goto .x
            label .x
        return a.count, lst

    assert run_async(func(), [1, 2, 3, 4]) == (4, [1, 2, 3, 4])


def test_jump_into_async_for_and_live():
    @with_goto
    async def func():
        a = Awaiter()
        ai = AsyncIterator(a)
        lst = []
        j = 0
        for i in range(4):
            goto.param .x = ai
            async for j in None:
                label .x
                lst.append(j)
        return a.count, i, lst

    async_args = [1, 2, 3, None, 4, 5, None, 6, None, None]
    list_results = [0, 1, 2, 3, 3, 4, 5, 5, 6, 6]
    assert run_async(func(), async_args) == (10, 3, list_results)


def test_jump_into_async_for_and_live_param_iterable():
    @with_goto
    async def func():
        a = Awaiter()
        ai = AsyncIterable(a)
        lst = []
        j = 0
        for i in range(4):
            goto.param .x = ai
            async for j in None:
                label .x
                lst.append(j)
        return a.count, i, lst

    async_args = [1, 2, 3, None, 4, 5, None, 6, None, None]
    list_results = [0, 1, 2, 3, 3, 4, 5, 5, 6, 6]
    assert run_async(func(), async_args) == (10, 3, list_results)


class AsyncContext:
    def __init__(self, awaiter):
        self.enters = 0
        self.exits = 0
        self.awaiter = awaiter

    async def __aenter__(self):
        self.enters += 1
        await self.awaiter
        return self

    async def __aexit__(self, a, b, c):
        self.exits += 1
        await self.awaiter
        return self

    def data(self):
        return (self.enters, self.exits)


def test_jump_out_of_async_with_and_survive():
    @with_goto
    async def func():
        a = Awaiter()
        ac = AsyncContext(a)
        for i in range(10):
            async with ac:
                goto .x
            label .x
        return a.count, ac.data()

    assert run_async(func()) == (10, (10, 0))


def test_jump_out_of_async_with_and_live():
    @with_goto
    async def func():
        a = Awaiter()
        ac = AsyncContext(a)
        for i in range(10):
            for j in range(10):
                async with ac:
                    goto .x
            label .x
            await a
        return a.count, ac.data()

    assert run_async(func()) == (20, (10, 0))


def test_jump_into_async_with_and_live():
    @with_goto
    async def func():
        a = Awaiter()
        ac = AsyncContext(a)
        for i in range(10):
            goto.param .x = ac
            async with ac:
                label .x
            await a
        return a.count, ac.data()

    assert run_async(func()) == (20, (0, 10))
