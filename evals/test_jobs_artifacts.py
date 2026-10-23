"""作业层 + 产物中心 端到端门禁（离线，不碰任何真设备）。

验证四层：
  ① 作业状态机：提交 → 领活 → 掉线回收 → 换节点重跑 → 成功/失败/取消
  ② 产物仓库：内容寻址、同内容去重、丢文件如实报错、TTL 回收
  ③ 注册表集成：job.*/artifact.* 进能力表（只读可调、副作用无闸门 fail-closed）
  ④ MCP 暴露：只读默认导出、副作用仅 --unsafe 导出

为什么用真 catalog.build()：前几轮踩过「测试桩全绿、真表没接上」的坑，
只有真表能证明「她/模型真的看得见这几项能力」。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


# 隔离到临时目录，绝不碰用户真实的 ~/.fleet
TMP = tempfile.mkdtemp(prefix="fleet_ja_")
os.environ["FLEET_JOBS"] = os.path.join(TMP, "jobs.json")
os.environ["FLEET_ARTIFACTS"] = os.path.join(TMP, "arts")
os.environ["FLEET_DEVICES"] = os.path.join(TMP, "devices.json")
os.environ["FLEET_MEMORY"] = "0"

from fleet import jobs as J            # noqa: E402
from fleet import artifacts as A       # noqa: E402

# ================================================================ ① 作业状态机
print("① 作业状态机")
st = J.JobStore(path=os.path.join(TMP, "jobs.json"))

j = st.submit("image.gen", {"prompt": "一只蓝鲸"}, max_attempts=2)
check(j["state"] == J.QUEUED, "提交后是 queued")
check(st.get(j["id"])["cap"] == "image.gen", "能按 id 查回")
check([x["id"] for x in st.list()] == [j["id"]], "list 能列出")
check(st.claim("phoneA", caps={"video.gen"}) is None, "干不了的能力不许派下去")

got = st.claim("phoneA", caps={"image.gen"})
check(bool(got) and got["state"] == J.RUNNING and got["attempts"] == 1, "领走 → running，记第 1 次尝试")
check(got["lease_until"] > __import__("time").time(), "领走后有租约")
check(st.heartbeat(got["id"]) is True, "续租成功")

reaped = st.reap(now=__import__("time").time() + J.DEFAULT_LEASE + 10)
check(len(reaped) == 1 and reaped[0]["state"] == J.QUEUED, "租约过期 → 退回 queued")
check(reaped[0]["node"] == "", "退回后清空节点（允许换机重跑）")

got2 = st.claim("phoneB", caps={"image.gen"})
check(bool(got2) and got2["attempts"] == 2, "换台手机接着跑，尝试次数累加")
fin = st.finish(got2["id"], False, err="手机没电了")
check(fin["state"] == J.FAILED and "没电" in fin["result"]["err"], "试满次数 → failed 且带原因")
check(st.finish(got2["id"], True, data={})["state"] == J.FAILED, "重复回报幂等，不改终态")

j2 = st.submit("video.gen", {"prompt": "海浪"}, node="pc1")
st.claim("pc1", caps={"video.gen"})
done = st.finish(st.list(state=J.RUNNING)[0]["id"], True,
                 data={"path": "/tmp/a.mp4"}, artifacts=["art-abc"])
check(done["state"] == J.DONE and done["artifacts"] == ["art-abc"], "成功 → done 且挂上产物")

j3 = st.submit("exec", {"cmd": "echo hi"})
ok_c, _ = st.cancel(j3["id"])
check(ok_c and st.get(j3["id"])["state"] == J.CANCELLED, "排队中可取消")
check(st.cancel(j3["id"])[0] is False, "终态再取消如实说不必取消")

# pump：注入假 dispatch，验证「领活→执行→结算」整条链
j4 = st.submit("image.gen", {"prompt": "猫"})
ran = st.pump(lambda job: (True, {"path": "/tmp/cat.png"}, "", ["art-cat"]),
              node="phoneC", caps={"image.gen"}, max_jobs=5)
check(len(ran) == 1 and ran[0]["state"] == J.DONE, "pump 跑完即结算")
check(st.get(j4["id"])["artifacts"] == ["art-cat"], "pump 把产物挂到作业上")


def boom(job):
    raise RuntimeError("派发炸了")


j5 = st.submit("exec", {"cmd": "x"}, max_attempts=1)
out = st.pump(boom, node="phoneC", caps={"exec"}, max_jobs=1)
check(bool(out) and out[0]["state"] == J.FAILED and "派发炸了" in out[0]["result"]["err"],
      "dispatch 抛异常也结算成 failed（不留 running 僵尸）")

# 永久失败（retry=False）只尝试一次——未知能力不该让别的节点白跑
j6 = st.submit("nope.cap", {}, max_attempts=5)
out6 = st.pump(lambda job: (False, {}, "不认识这个能力", None, False),
               node="phoneD", caps={"nope.cap"}, max_jobs=3)
check(len(out6) == 1 and out6[0]["state"] == J.FAILED and out6[0]["attempts"] == 1,
      "永久失败立刻判死，只尝试 1 次（不浪费别的节点）")

st2 = J.JobStore(path=os.path.join(TMP, "jobs.json"))
check(st2.get(j3["id"])["state"] == J.CANCELLED, "落盘可重读（进程重启不丢账）")

# ================================================================ ② 产物仓库
print("② 产物仓库")
arts = A.ArtifactStore(root=os.path.join(TMP, "arts"))
a1 = arts.put_bytes(b"PNG-fake", name="a.png", kind="image", source="phoneA", job="job-1")
check(a1["id"].startswith("art-") and len(a1["id"]) == 20, "id 是 art- + 16 位哈希")
check(arts.open_bytes(a1["id"]) == b"PNG-fake", "取回内容一致")
a2 = arts.put_bytes(b"PNG-fake", name="b.png", source="phoneB")
check(a2["id"] == a1["id"], "同内容同 id（去重）")
check(set(arts.get(a1["id"])["sources"]) == {"phoneA", "phoneB"}, "去重但保留全部来源")
check(len(arts.list()) == 1, "去重后条目仍是 1")

try:
    arts.put_bytes(b"")
    check(False, "空数据应拒收")
except ValueError as e:
    check("空" in str(e), "空数据拒收并带原因")

lost = arts.put_bytes(b"gone", kind="blob")
os.remove(lost["path"])
try:
    arts.open_bytes(lost["id"])
    check(False, "实体丢失应报错")
except FileNotFoundError as e:
    check("不见" in str(e), "索引在但文件丢了 → 如实说文件不见了")

old = arts.put_bytes(b"expired", kind="blob", ttl_days=1)
arts._idx[old["id"]]["created_at"] = __import__("time").time() - 3 * 86400
removed, freed = arts.gc()
check(removed == 1 and freed >= 4, "TTL 到期被 gc 清掉")

# ================================================================ ③ 注册表集成
print("③ 注册表集成（真 catalog.build）")
from fleet.catalog import build        # noqa: E402

reg = build()
names = {c.name for c in reg.capabilities()}
need = {"job.submit", "job.list", "job.get", "job.cancel",
        "artifact.put", "artifact.get", "artifact.list"}
check(not (need - names), "7 项作业/产物能力全进能力表（缺：%s）" % (need - names or "无"))
check(len(names) >= 99, "能力总数 ≥ 99（实际 %d）" % len(names))

r = reg.call("job.list", args={"limit": 3})
check(r.ok, "只读 job.list 可调")
r = reg.call("artifact.list", args={})
check(r.ok, "只读 artifact.list 可调")
r = reg.call("job.submit", args={"cap": "image.gen"})
check(not r.ok and "闸门" in r.err, "副作用无闸门 → fail-closed（不许悄悄执行）")

# ================================================================ ④ MCP 暴露
print("④ MCP 暴露")
from fleet.mcp.server import MCPServer  # noqa: E402

srv = MCPServer(registry=reg)
tn = {t["name"] for t in srv.tools()["tools"]}
check({"job_list", "job_get", "artifact_get", "artifact_list"} <= tn,
      "只读能力默认导出给外部 AI 客户端")
check("job_submit" not in tn and "artifact_put" not in tn,
      "副作用能力默认不导出（stdio 没弹窗可等审批）")
srv_u = MCPServer(registry=reg, include_unsafe=True)
tun = {t["name"] for t in srv_u.tools()["tools"]}
check({"job_submit", "job_cancel", "artifact_put"} <= tun, "--unsafe 才导出副作用能力")

# 说明书与别名
import capabilities as C                # noqa: E402

check(C.spec_of("job.submit")["title"] == "提交长任务（作业）", "说明书标题正确")
check(C.param_names("job.submit") == ["cap"], "job.submit 必填只有 cap")
check(C.spec_of("job.run")["title"] == "提交长任务（作业）", "别名 job.run 可用")
check(C.spec_of("art.get")["title"] == "取产物", "别名 art.get 可用")

# ================================================================ 收尾
import shutil                           # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
print("test_jobs_artifacts:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
