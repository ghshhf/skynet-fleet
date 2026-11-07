package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 端点配置是纯数据：不预设厂商、不写死地址，界面上怎么填就怎么存。 */
class LlmEndpointTest {

    @Test
    fun `别名全部兼容`() {
        val s = LlmSettings.of(
            JSONObject()
                .put("name", "自定义")
                .put("base_url", "https://my.example.com/v1")
                .put("api_key", "k-123")
                .put("model_name", "my-chat"),
        )
        assertEquals("自定义", s.label)
        assertEquals("https://my.example.com/v1", s.base)
        assertEquals("k-123", s.key)
        assertEquals("my-chat", s.model)
        assertTrue(s.ready)
    }

    @Test
    fun `往返不丢字段`() {
        val s = LlmSettings("ep-1", "甲", "https://a.cn/v1", "key", "m1")
        val back = LlmSettings.of(s.json())
        assertEquals(s, back)
    }

    @Test
    fun `地址归一化与显示名`() {
        assertEquals("https://a.cn/v1", LlmSettings(base = " https://a.cn/v1/// ").baseNorm)
        assertEquals("a.cn", LlmSettings(base = "https://a.cn/v1").title())
        assertEquals("我的端点", LlmSettings(label = "我的端点", base = "https://a.cn/v1").title())
    }

    @Test
    fun `缺模型不算就绪`() {
        assertTrue(LlmSettings(base = "https://a.cn/v1", key = "k").let { !it.ready })
        assertTrue(LlmSettings(base = "https://a.cn/v1", key = "k", model = "m").ready)
        assertFalse(LlmSettings().ready)
    }

    @Test
    fun `自动挑选只认免费对话模型`() {
        val names = listOf(
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "zai-org/GLM-5.3-Flash",
            "BAAI/bge-m3",
            "Qwen/Qwen3-8B",
            "Qwen/Qwen2.5-7B-Instruct",
        )
        assertEquals("Qwen/Qwen3-8B", LlmConfig.pickFreeModel(names))
    }

    @Test
    fun `全是付费模型时留空不自动填`() {
        val names = listOf(
            "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4.1-Flash",
            "Pro/Qwen/Qwen2.5-7B-Instruct", "moonshotai/Kimi-K2.7-Code",
        )
        assertTrue(LlmConfig.pickFreeModel(names) == null)
    }

    @Test
    fun `清单里没有免费档时一个都不自动填`() {
        // 有的端点 /models 里全是付费 / 工具模型，零免费档、零 free 字样。
        // 这类端点必须留空，不能按「清单里第一个」或「名字像便宜」自动填。
        val names = listOf(
            "vendor/chat-pro", "vendor/chat-flash-paid", "vendor/vl-38b",
            "vendor/embedding-large", "vendor/reranker-v2", "vendor/image-gen",
        )
        assertTrue(LlmConfig.pickFreeModel(names) == null)
    }

    @Test
    fun `免费标记可用且非对话模型被排除`() {
        assertEquals(
            "some-chat-free",
            LlmConfig.pickFreeModel(listOf("BAAI/bge-m3", "FunAudioLLM/SenseVoiceSmall", "some-chat-free")),
        )
        assertTrue(LlmConfig.pickFreeModel(listOf("BAAI/bge-reranker-v2-m3")) == null)
    }
}
