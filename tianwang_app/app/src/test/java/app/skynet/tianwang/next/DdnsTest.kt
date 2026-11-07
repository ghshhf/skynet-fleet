package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DdnsTest {
    private val ip = "2408:8000:abcd::1"

    private fun cfg(
        provider: String = Ddns.PROVIDER_DUCK,
        domain: String = "myphone",
        token: String = "tok-123",
        url: String = "",
        enabled: Boolean = true,
    ) = Ddns.Cfg(provider, domain, token, url, enabled)

    @Test
    fun duckDomainAcceptsBareAndFullName() {
        assertEquals("myphone", Ddns.normalizeDuckDomain("myphone"))
        assertEquals("myphone", Ddns.normalizeDuckDomain("MyPhone.duckdns.org"))
        assertEquals("my-phone", Ddns.normalizeDuckDomain(" my-phone.duckdns.org "))
        assertNull(Ddns.normalizeDuckDomain(""))
        assertNull(Ddns.normalizeDuckDomain("bad_domain"))
        assertNull(Ddns.normalizeDuckDomain("-bad"))
    }

    @Test
    fun duckUrlCarriesIpv6AndToken() {
        val url = Ddns.updateUrl(cfg(), ip)!!
        assertTrue(url.startsWith("https://www.duckdns.org/update?"))
        assertTrue(url.contains("domains=myphone"))
        assertTrue(url.contains("ipv6=2408%3A8000%3Aabcd%3A%3A1"))
        assertTrue(url.contains("token=tok-123"))
    }

    @Test
    fun duckUrlRefusesIncompleteConfig() {
        assertNull(Ddns.updateUrl(cfg(domain = ""), ip))
        assertNull(Ddns.updateUrl(cfg(token = ""), ip))
    }

    @Test
    fun customTemplateReplacesPlaceholders() {
        val c = cfg(
            provider = Ddns.PROVIDER_CUSTOM,
            domain = "",
            token = "a b",
            url = "https://ddns.example.com/update?ip={ipv6}&k={token}",
        )
        val url = Ddns.updateUrl(c, ip)!!
        assertTrue(url.contains("ip=2408%3A8000%3Aabcd%3A%3A1"))
        assertTrue(url.contains("k=a+b"))
        assertFalse(url.contains("{ipv6}"))
    }

    @Test
    fun customTemplateRequiresIpv6Placeholder() {
        val c = cfg(provider = Ddns.PROVIDER_CUSTOM, url = "https://ddns.example.com/update")
        assertNull(Ddns.updateUrl(c, ip))
    }

    @Test
    fun offProviderHasNoUrl() {
        assertNull(Ddns.updateUrl(cfg(provider = Ddns.PROVIDER_OFF), ip))
    }

    @Test
    fun nameOfShowsStableNameWithoutToken() {
        assertEquals("myphone.duckdns.org", Ddns.nameOf(cfg()))
        val custom = cfg(
            provider = Ddns.PROVIDER_CUSTOM,
            token = "secret",
            url = "https://ddns.example.com/secret/update?ip={ipv6}",
        )
        val name = Ddns.nameOf(custom)
        assertFalse(name.contains("secret"))
    }
}
