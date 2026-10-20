"""资源余量计算测试：AllocTRES 语义、节点/分区聚合、四类过滤。"""

from clu.slurm.commands import expand_hostlist, parse_gres, resources
from clu.ssh.connection import ExecResult

# 镜像贵大真实格式：gres/gpu=N（总量）与 gres/gpu:TYPE=M（显式指名）并存
NODES_SCONTROL = (
    " NodeName=gpu01 CPUAlloc=8 CPUTot=64 RealMemory=512000 AllocMem=256000 State=MIXED"
    " AllocTRES=cpu=8,gres/gpu=2,gres/gpu:A100=1 Gres=gpu:A100:8(S:0-1) Partitions=gpu \n"
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
    assert expand_hostlist("gpu[01-02]") == ["gpu01", "gpu02"]
    assert expand_hostlist("a,b") == ["a", "b"]
    assert expand_hostlist("cpu[1-2],gpu3") == ["cpu1", "cpu2", "gpu3"]
    assert expand_hostlist("") == []


def test_parse_gres() -> None:
    assert parse_gres("gpu:A100:8(S:0-1)") == {"A100": 8}
    assert parse_gres("gpu:A100:8(S:0-1),gpu:4090:2") == {"A100": 8, "4090": 2}
    assert parse_gres("gpu:4") == {"_": 4}
    assert parse_gres("(null)") == {}
    assert parse_gres("shard:8") == {}


def test_alloc_tres_typeless_total_wins() -> None:
    """总量 gres/gpu=2 优先于 typed gres/gpu:A100=1（贵大实测语义）。"""
    from clu.slurm.commands import _alloc_tres_gpus

    assert _alloc_tres_gpus("cpu=8,gres/gpu=2,gres/gpu:A100=1", {"A100": 8}) == {"A100": 2}
    assert _alloc_tres_gpus("cpu=64,gres/gpu:4090=4", {"4090": 4}) == {"4090": 4}
    # 无型号总量 + 多型号：差额均摊
    split = _alloc_tres_gpus("gres/gpu=3", {"a": 2, "b": 2})
    assert sum(split.values()) == 3
    assert _alloc_tres_gpus("cpu=48", {"a": 2}) == {}


async def test_resources_aggregation() -> None:
    result = await resources(make_runner(), include_nodes=True)  # type: ignore[arg-type]
    nodes = {n["node"]: n for n in result["nodes"]}
    assert nodes["gpu01"]["cpu"] == {"total": 64, "allocated": 8, "free": 56}
    assert nodes["gpu01"]["gpus"]["A100"] == {"total": 8, "allocated": 2, "free": 6}
    assert nodes["gpu02"]["fully_idle"] is True
    assert nodes["gpu02"]["gpus"]["A100"]["free"] == 8
    assert nodes["gpu03"]["gpus"]["4090"]["free"] == 0
    assert nodes["bad01"]["usable"] is False

    parts = result["partitions"]
    assert parts["gpu"]["nodes"] == 3
    assert parts["gpu"]["gpus"]["A100"] == {"total": 24, "allocated": 2, "free": 22}
    assert parts["gpu2"]["gpus"]["4090"]["free"] == 0
    assert parts["cpu"]["cpu"]["free"] == 48


async def test_resources_four_filters() -> None:
    base = make_runner()
    cpu_free = await resources(base, flt="cpu_free")  # type: ignore[arg-type]
    assert {n["node"] for n in cpu_free["nodes"]} == {"gpu01", "gpu02", "cpu01"}

    both = await resources(base, flt="cpu_gpu_free")  # type: ignore[arg-type]
    assert {n["node"] for n in both["nodes"]} == {"gpu01", "gpu02"}

    idle = await resources(base, flt="fully_idle")  # type: ignore[arg-type]
    assert {n["node"] for n in idle["nodes"]} == {"gpu02", "cpu01"}

    gpus = await resources(base, gpu_only=True)  # type: ignore[arg-type]
    assert {n["node"] for n in gpus["nodes"]} == {"gpu01", "gpu02", "gpu03", "bad01"}
    assert set(gpus["partitions"]) == {"gpu", "gpu2"}

    combined = await resources(base, gpu_only=True, flt="fully_idle")  # type: ignore[arg-type]
    assert {n["node"] for n in combined["nodes"]} == {"gpu02"}


async def test_resources_compact_by_default() -> None:
    result = await resources(make_runner())  # type: ignore[arg-type]
    assert result["nodes"] == []
    assert result["node_count"] == 5
    assert "AllocTRES" in str(result["note"])
