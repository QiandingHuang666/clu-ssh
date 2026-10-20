"""资源余量计算测试：hostlist 展开、GRES 解析、节点/分区聚合、三种过滤。"""

from clu.slurm.commands import expand_hostlist, parse_gres, resources
from clu.ssh.connection import ExecResult

NODES_SCONTROL = (
    " NodeName=gpu01 CPUAlloc=8 CPUTot=64 RealMemory=512000 AllocMem=256000 State=MIXED"
    " AllocTRES=cpu=8,gres/gpu=2 Gres=gpu:A100:8(S:0-1) Partitions=gpu \n"
    " NodeName=gpu02 CPUAlloc=0 CPUTot=64 RealMemory=512000 AllocMem=0 State=IDLE"
    " Gres=gpu:A100:8(S:0-1) Partitions=gpu \n"
    " NodeName=gpu03 CPUAlloc=64 CPUTot=64 RealMemory=512000 AllocMem=512000 State=ALLOCATED"
    " AllocTRES=cpu=64,gres/gpu:4090=4 Gres=gpu:4090:4(S:0-1) Partitions=gpu2 \n"
    " NodeName=cpu01 CPUAlloc=0 CPUTot=48 RealMemory=256000 AllocMem=0 State=IDLE"
    " Gres=(null) Partitions=cpu \n"
    " NodeName=bad01 CPUAlloc=0 CPUTot=48 RealMemory=256000 AllocMem=0 State=DOWN+DRAIN"
    " AllocTRES= Gres=gpu:A100:8 Partitions=gpu \n"
)


def make_runner() -> object:
    async def run(command: str, *, timeout: float | None = None) -> ExecResult:
        return ExecResult(command, 0, NODES_SCONTROL, "", 1)

    return run


def test_expand_hostlist() -> None:
    assert expand_hostlist("node[1-3,5]") == ["node1", "node2", "node3", "node5"]
    assert expand_hostlist("gpu[01-02]") == ["gpu01", "gpu02"]  # 保留前导零宽度
    assert expand_hostlist("a,b") == ["a", "b"]
    assert expand_hostlist("single") == ["single"]
    assert expand_hostlist("") == []


def test_parse_gres() -> None:
    assert parse_gres("gpu:A100:8(S:0-1)") == {"A100": 8}
    assert parse_gres("gpu:A100:8(S:0-1),gpu:4090:2") == {"A100": 8, "4090": 2}
    assert parse_gres("gpu:4") == {"_": 4}
    assert parse_gres("(null)") == {}
    assert parse_gres("shard:8") == {}


async def test_resources_aggregation() -> None:
    result = await resources(make_runner(), include_nodes=True)  # type: ignore[arg-type]
    nodes = {n["node"]: n for n in result["nodes"]}
    assert nodes["gpu01"]["cpu"] == {"total": 64, "allocated": 8, "free": 56}
    assert nodes["gpu01"]["gpus"]["A100"]["free"] == 6  # 8 - 运行作业 2
    assert nodes["gpu02"]["fully_idle"] is True
    assert nodes["gpu02"]["gpus"]["A100"]["free"] == 8
    assert nodes["gpu03"]["gpus"]["4090"]["free"] == 0
    assert nodes["bad01"]["usable"] is False

    parts = result["partitions"]
    assert parts["gpu"]["nodes"] == 3
    assert parts["gpu"]["idle_nodes"] == 1
    assert parts["gpu"]["gpus"]["A100"] == {
        "total": 24,
        "allocated": 2,
        "free": 22,
    }  # 2 来自 AllocTRES gres/gpu=2
    assert parts["gpu2"]["gpus"]["4090"]["free"] == 0
    assert parts["cpu"]["cpu"]["free"] == 48


async def test_resources_filters() -> None:
    base = make_runner()
    idle = await resources(base, only_idle=True)  # type: ignore[arg-type]
    assert [n["node"] for n in idle["nodes"]] == ["gpu02", "cpu01"]

    free = await resources(base, only_free=True)  # type: ignore[arg-type]
    names = [n["node"] for n in free["nodes"]]
    assert "gpu01" in names and "gpu03" not in names and "bad01" not in names

    gpu_only = await resources(base, partition="gpu")  # type: ignore[arg-type]
    assert {n["node"] for n in gpu_only["nodes"]} == {"gpu01", "gpu02", "bad01"}

    idle_gpu = await resources(base, partition="gpu", only_idle=True)  # type: ignore[arg-type]
    assert [n["node"] for n in idle_gpu["nodes"]] == ["gpu02"]


async def test_resources_compact_by_default() -> None:
    """默认紧凑：nodes 为空、node_count 齐全。"""
    result = await resources(make_runner())  # type: ignore[arg-type]
    assert result["nodes"] == []
    assert result["node_count"] == 5
    assert "AllocTRES" in str(result["note"])


def test_alloc_tres_typeless_vs_typed() -> None:
    from clu.slurm.commands import _alloc_tres_gpus

    assert _alloc_tres_gpus("cpu=8,gres/gpu=2", {"A100": 8}) == {"A100": 2}
    assert _alloc_tres_gpus("cpu=64,gres/gpu:4090=4", {"4090": 4}) == {"4090": 4}
    # 无型号 + 多型号：均摊
    assert _alloc_tres_gpus("gres/gpu=3", {"a": 2, "b": 2}) in ({"a": 2, "b": 1}, {"a": 1, "b": 2})
    assert _alloc_tres_gpus("cpu=48", {"a": 2}) == {}
