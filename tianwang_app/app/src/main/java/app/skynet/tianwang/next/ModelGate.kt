package app.skynet.tianwang.next

import java.util.concurrent.atomic.AtomicBoolean

/**
 * 本地模型闸门：同一台手机任何时刻只跑一路生成。
 *
 * llama.cpp 的句柄缓存是进程级的，两个协程同时进 generate 会抢同一个 KV；
 * 更糟的是发热翻倍、速度反而更慢。所以「整单目标」和「单句问答」共用这一道闸。
 */
object ModelGate {
    private val busy = AtomicBoolean(false)

    fun enter(): Boolean = busy.compareAndSet(false, true)

    fun exit() {
        busy.set(false)
    }
}
