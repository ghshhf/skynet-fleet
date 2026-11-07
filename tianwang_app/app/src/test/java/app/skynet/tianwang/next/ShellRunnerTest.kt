package app.skynet.tianwang.next

import org.junit.Assert.assertTrue
import org.junit.Test

/** shell 执行器：退出码、stderr 合并、超时终止、输出截断。跑的是真的 `sh`，不是 mock。 */
class ShellRunnerTest {
    @Test
    fun capturesStdoutAndExitCode() {
        val out = ShellRunner.run("echo hello-skynet", "", 5_000)
        assertTrue(out.startsWith("exit=0"))
        assertTrue(out.contains("hello-skynet"))
    }

    @Test
    fun mergesStderrAndReportsNonZeroExit() {
        val out = ShellRunner.run("echo oops 1>&2; exit 7", "", 5_000)
        assertTrue(out.startsWith("exit=7"))
        assertTrue(out.contains("oops"))
    }

    @Test
    fun killsOnTimeout() {
        val started = System.currentTimeMillis()
        val out = ShellRunner.run("sleep 5", "", 500)
        val cost = System.currentTimeMillis() - started
        assertTrue("应报超时，实际：$out", out.contains("超时"))
        assertTrue("应在超时后尽快返回，实际 ${cost}ms", cost < 4_000)
    }

    @Test
    fun truncatesHugeOutput() {
        val out = ShellRunner.run("head -c 20000 /dev/zero | tr '\\0' 'a'", "", 5_000)
        assertTrue(out.contains("已截断"))
        assertTrue("截断后仍应远小于原始输出", out.length < ShellRunner.MAX_OUTPUT + 200)
    }
}
