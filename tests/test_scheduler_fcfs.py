from gcos.kernel.pcb import AgentControlBlock
from gcos.kernel.pid_alloc import PidAllocator
from gcos.kernel.ready_queue import ReadyQueue
from gcos.kernel.scheduler import (
    FCFSScheduler,
    PriorityScheduler,
    make,
)


def make_pcb(alloc: PidAllocator, name: str, priority: int = 5) -> AgentControlBlock:
    return AgentControlBlock(
        pid=alloc.next(), name=name, prompt=f"prompt for {name}", priority=priority
    )


def test_fcfs_returns_in_insertion_order():
    alloc = PidAllocator()
    q = ReadyQueue()
    a = make_pcb(alloc, "a")
    b = make_pcb(alloc, "b")
    c = make_pcb(alloc, "c")
    for p in (a, b, c):
        q.put(p)

    sched = FCFSScheduler()
    assert sched.pick_next(q).name == "a"
    assert sched.pick_next(q).name == "b"
    assert sched.pick_next(q).name == "c"
    assert sched.pick_next(q) is None


def test_priority_picks_highest_first():
    alloc = PidAllocator()
    q = ReadyQueue()
    low = make_pcb(alloc, "low", priority=1)
    high = make_pcb(alloc, "high", priority=9)
    mid = make_pcb(alloc, "mid", priority=5)
    for p in (low, high, mid):
        q.put(p)

    sched = PriorityScheduler()
    assert sched.pick_next(q).name == "high"
    assert sched.pick_next(q).name == "mid"
    assert sched.pick_next(q).name == "low"


def test_make_factory_known_schedulers():
    assert make("fcfs").name == "fcfs"
    assert make("priority").name == "priority"
    assert make("rr").name == "rr"


def test_make_factory_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        make("nope")


def test_pid_allocator_is_monotonic():
    a = PidAllocator(start=100)
    pids = [a.next() for _ in range(5)]
    assert pids == [100, 101, 102, 103, 104]
