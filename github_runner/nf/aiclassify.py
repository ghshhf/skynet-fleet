# -*- coding: utf-8 -*-
"""
nf/aiclassify.py —— AI 词法增强分类（补 classify.py 规则覆盖不足）
设计：主人要求"AI 直接分类，别等 probe"。
  第一层（本脚本，机械化）：把领域词典 + TLD 结构启发式跑一遍，覆盖肉眼可断的大簇；
  第二层（主人在会话里亲自判）：本脚本把剩余疑难导出到 data/_pending_domains.txt，
  主人分批读入上下文直接标，判断写进 data/_ai_manual.json（domain -> 类别）。
结果全部并入 data/classified.json（basis 标 ai-lex / ai-manual，绝不删除任何条目）。

用法：
  python aiclassify.py            # 跑词法层 + 导出待判清单
  python aiclassify.py --merge    # 只把 _ai_manual.json 并进 classified.json
"""
import json
import os
import re
import sys
from collections import Counter

# 落盘走 store（唯一临时名 + fsync）：这个文件有 1.4 万条，覆写时被打断就是全丢。
_FLEET_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _FLEET_ROOT not in sys.path:
    sys.path.insert(0, _FLEET_ROOT)
from fleet import store  # noqa: E402

BASE = os.environ.get("NETFORCE_OUT") or os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(BASE, "resources.json")
CLS = os.path.join(BASE, "classified.json")
PENDING_TXT = os.path.join(BASE, "_pending_domains.txt")
MANUAL_JSON = os.path.join(BASE, "_ai_manual.json")

# ---------------------------------------------------------------- 词典
# (类别, [强词素]) —— 匹配域名主体（去掉 TLD 后的完整字符串），子串命中即归
LEX = [
    ("代码仓库/开源项目", [
        "github.io", "gitlab.io", "gitbook.io", "sourceforge", "gitea", "forgejo",
        "gitlab", "codeberg", "bitbucket", "git", "svn", "mercurial", "fossil",
    ]),
    ("框架/库文档", [
        "readthedocs", "docs.", "doc.", "documentation", "developer.", "devdocs",
        "api-docs", "godoc", "pkg.go.dev", "crates.io", "rubydoc", "ruby-doc",
        "jsdoc", "pydoc", "javadoс", "javadoc", "apidock", "apireference",
        "swagger", "openapi", "stoplight", "readme.io", "gitbook", "docusaurus",
        "sphinx", "mkdocs", "dash-docs",
    ]),
    ("高校/实验室", [
        ".edu", "univ", "universidad", "universitat", "universite", "universität",
        ".ac.", "academy.edu", "college", "institute.", "edu.cn", "edu.au",
        "csail", "mit.edu", "stanford", "berkeley", "cmu.edu", "caltech",
        "ethz", "epfl", "ox.ac", "cam.ac", "imperial", "tsinghua", "peking",
        "fudan", "sjtu", "zju", "nju", "ustc", "hit.edu", "xjtu", "hust",
        "sustech", "nus.edu", "ntu.edu", "kaist", "eth.", "tum.de", "rwth",
        "ucla", "usc.edu", "nyu.edu", "cornell", "princeton", "yale", "harvard",
        "gatech", "umich", "uiuc", "illinois", "purdue", "duke", "jhu",
        "washu", "utexas", "asu.edu", "ucsd", "ucdavis", "uci.edu", "uchicago",
        "columbia", "upenn", "brown.edu", "dartmouth", "northwestern", "vanderbilt",
        "laboratory", "lab-", "-lab.", "labs.", "research.", "cil.", "cvlab",
        "robotics.", "ai lab", "department", "dept.", "faculty", "cs.",
    ]),
    ("标准/联盟/公益组织", [
        ".gov", "gov.cn", "gov.uk", "who.int", "un.org", "w3.org", "ietf",
        "iso.org", "nist", "ansi", "ieee", "itu", "ecma", "rfc-editor",
        "openstand", "consortium", "alliance", "foundation", ".org/foundation",
        "nonprofit", "non-profit", "charity", "ngo.", "unicef", "worldbank",
        "oecd", "europa.eu", "esa.int", "nasa",
    ]),
    ("网络安全/信息安全", [
        "cve", "cwe", "exploit", "0day", "pentest", "offsec", "hackthebox",
        "tryhackme", "crackstation", "hashcat", "johntheripper", "burp", "nmap",
        "metasploit", "cobaltstrike", "bloodhound", "impacket", "responder",
        "wireshark", "tcpdump", "suricata", "snort", "zeek", "osquery",
        "velociraptor", "dfir", "forensics", "malware", "ransomware", "botnet",
        "phishing", "spamhaus", "abuseipdb", "grey noise", "greynoise", "shodan",
        "censys", "zoomeye", "fofa", "quake.360", "hackerone", "bugcrowd",
        "synack", "intigriti", "yeswehack", "vulners", "vuldb", "vuln",
        "exploit-db", "packetstorm", "seclists", "payloadsallthethings",
        "owasp", "portswigger", "sec.", "security", "secure.", "defcon",
        "blackhat", "bsides", "ctf", "ctftime", "picoctf", "root-me",
    ]),
    ("OSINT/情报调查", [
        "osint", "bellingcat", "maltego", "spiderfoot", "theharvester",
        "recon-ng", "amass", "subfinder", "assetfinder", "crt.sh", "certspotter",
        "intelx", "dehashed", "leakpeek", "haveibeen", "pwndb", "ephemeral",
        "dork", "footprint", "investigation", "due-diligence", "background check",
    ]),
    ("数据服务/查询工具", [
        "whois", "ipinfo", "ip2location", "ip-api", "ipapi", "maxmind",
        "ipgeolocation", "ipify", "icanhazip", "ifconfig.me", "whatismyip",
        "dnsdumpster", "dnsspy", "securitytrails", "passivetotal", "riskiq",
        "builtwith", "wappalyzer", "similarweb", "semrush", "ahrefs", "moz",
        "majestic", "sistrix", "screamingfrog", "crunchbase", "pitchbook",
        "cbinsights", "tracxn", "mattermark", "glassdoor", "indeed.", "zoominfo",
        "apollo.io", "clearbit", "fullcontact", "pipl", "spokeo", "beenverified",
        "socialcatfish", "thatsthem", "fastpeoplesearch", "whitepages",
        "flightradar", "flightaware", "marinetraffic", "vesselfinder",
        "shiptracker", "adsbexchange", "flightstats", "weather.gov", "noaa",
        "earthdata", "sentinel", "copernicus", "modis", "landsat", "usgs",
    ]),
    ("论文预印本/学术检索", [
        "arxiv", "biorxiv", "medrxiv", "chemrxiv", "socarxiv", "psyarxiv",
        "zenodo", "figshare", "dryad", "osf.io", "ssrn", "repec", "ideas.repec",
        "semanticscholar", "connectedpapers", "scite.ai", "scinapse", "f1000",
        "pubpeer", "jstor", "sci-hub", "sciencedirect", "springer", "tandfonline",
        "wiley", "mdpi", "frontiersin", "plos", "nature.com", "science.org",
        "cell.com", "thelancet", "bmj.com", "nejm", "jama", "apa.org",
        "scholar", "citation", "doi.org", "crossref", "orcid", "researcherid",
        "publons", "scopus", "web of science", "dimensions.ai", "lens.org",
    ]),
    ("数据集", [
        "dataset", "kaggle", "openml", "uc Irvine", "uci.edu/ml", "huggingface",
        "data.gov", "opendata", "datahub", "data.world", "quandl", "nasdaq data",
        "yelp dataset", "movielens", "grouplens", "imagenet", "coco dataset",
        "openimages", "visualgenome", "cityscapes", "kitti", "waymo", "nuscenes",
        "commoncrawl", "wikipedia dump", "wikidata", "gutenberg", "corpus",
        "annotated", "benchmark", "labeled data",
    ]),
    ("大模型/模型库", [
        "modelscope", "civitai", "liblib", "hf-mirror", "modelzoo", "model-zoo",
        "model zoo", "checkpoint", "weights", "llm", "safetensors", "gguf",
        "ollama", "lmstudio", "llama.cpp", "vllm", "text-generation",
        "opencompass", "eval harness",
    ]),
    ("AI工具/平台", [
        "openai", "anthropic", "chatgpt", "claude", "gemini", "copilot",
        "midjourney", "stablediffusion", "stable-diffusion", "comfyui",
        "automatic1111", "fooocus", "invokeai", "runwayml", "pika", "sora",
        "elevenlabs", "whisper", "bark", "tortoise-tts", "vall-e", "so-vits",
        "gpt", "llama", "mistral", "qwen", "deepseek", "chatglm", "baichuan",
        "yuanbao", "kimi", "moonshot", "zhipu", "sensetime", "megvii",
        "dify", "langchain", "llamaindex", "autogen", "crewai", "agentgpt",
        "openrouter", "together.ai", "groq", "fireworks", "replicate", "fal.ai",
        "venice", "perplexity", "poe.com", "character.ai", "spicychat",
        "janitorai", "sillytavern", "risuai", "flowyaipc", ".ai",
    ]),
    ("代理/匿名网络", [
        "proxy", "vpn", "shadowsocks", "v2ray", "xray", "trojan-go", "clash",
        "sing-box", "hysteria", "mihomo", "wireguard", "openvpn", "tor project",
        "torproject", "onion", "i2p", "freenet", "tails", "whonix", "qubes",
        "mullvad", "protonvpn", "nordvpn", "surfshark", "expressvpn", "windscribe",
        "tunnelbear", "privadovpn", "outline", "lantern", "psiphon", "gopher",
        "freegate", "ultrasurf", "vpngate", "socks", "antizapret", "nocensor",
    ]),
    ("邮件/邮箱服务", [
        "mail", "email", "smtp", "imap", "pop3", "mailbox", "inbox",
        "tempmail", "temp-mail", "10minutemail", "guerrillamail", "mailinator",
        "yopmail", "throwaway", "disposable", "burner", "addy", "anonaddy",
        "simplelogin", "firefox relay", "relay.firefox", "duck.com", "hide-my",
        "mailgun", "sendgrid", "postmark", "mailjet", "sendinblue", "brevo",
        "amazon ses", "ses.amazonaws", "mailchimp", "campaign monitor",
        "listmonk", "mailcow", "mailu", "postal", "stalwart", "dovecot",
        "roundcube", "rainloop", "snappy", "zimbra", "soyou", "mxroute",
        "purelymail", "migadu", "fastmail", "protonmail", "tutanota", "skiff",
    ]),
    ("虚拟卡/加密支付", [
        "virtualcard", "virtual-card", "cryptocard", "crypto-card", "redotpay",
        "dupay", "bybit card", "binance card", "wirex", "cryptopay", "paysafe",
        "prepaid", "debit card", "credit card", "fintech card",
    ]),
    ("加密/Web3", [
        "crypto", "blockchain", "bitcoin", "ethereum", "solana", "cardano",
        "polkadot", "avalanche", "polygon", "bnb", "tron", "litecoin", "monero",
        "ripple", "xrp", "dogecoin", "shiba", "defi", "dapp", "nft", "opensea",
        "metamask", "trustwallet", "ledger", "trezor", "exodus", "atomic wallet",
        "coinbase", "binance", "okx", "kucoin", "bybit", "gate.io", "bitget",
        "mexc", "htx", "kraken", "gemini.com", "bitstamp", "coingecko",
        "coinmarketcap", "etherscan", "blockchair", "blockexplorer", "explorer",
        "airdrop", "token", "staking", "yield", "liquidity", "swap", "dex",
        "cex", "bridge", "chainlist", "rpc", "web3", "solidity", "smart contract",
    ]),
    ("游戏/游戏资源", [
        "game", "gaming", "steam", "epicgames", "gog", "itch.io", "nexusmods",
        "curseforge", "modrinth", "spigotmc", "bukkit", "papermc", "purpur",
        "minecraft", "terraria", "factorio", "rimworld", "stardew", "noita",
        "ftl", "slay-the-spire", "slaythespire", "hollow knight", "hades",
        "repack", "fitgirl", "dodi", "skidrow", "codex", "cpul", "elamigos",
        "steamrip", "gamerun", "byrut", "anadius", "cs.rin", "gamelab",
        "rom", "emulator", "emulation", "retroarch", "dolphin-emu", "pcsx2",
        "rpcs3", "yuzu", "ryujinx", "cemu", "citra", "melonds", "mame",
        "vimm", "romsfun", "romhustler", "emuparadise", "coolrom", "gamulator",
    ]),
    ("电子书/图书", [
        "book", "ebook", "textbook", "library", "libgen", "z-lib", "zlibrary",
        "annas-archive", "anna-archive", "openlibrary", "gutenberg", "standardebooks",
        "manybooks", "free-ebooks", "pdfdrive", "booksc", "bookfinder", "goodreads",
        "scribd", "oreilly", "manning", "packtpub", "apress", "springer pub",
        "calibre", "kavita", "komga", "audiobook", "librivox", "podcastle",
    ]),
    ("课程/教程", [
        "course", "tutorial", "learn", "lessons", "classroom", "curriculum",
        "syllabus", "mooc", "coursera", "edx", "udemy", "udacity", "pluralsight",
        "datacamp", "codecademy", "freecodecamp", "theodinproject", "roadmap.sh",
        "w3schools", "javatpoint", "geeksforgeeks", "tutorialspoint", "tutorials",
        "bootcamp", "boot camp", "training", "certification", "exam", "quiz",
    ]),
    ("技术媒体/实战教程站", [
        "blog", "medium", "dev.to", "hashnode", "substack", "hackernoon",
        "kdnuggets", "towardsdatascience", "realpython", "real python",
        "machinelearningmastery", "tutorial", "guide", "how-to", "howto",
        "cheatsheet", "cheat-sheet", "newsletter", "daily.dev", "news.",
        "techcrunch", "theverge", "wired", "arstechnica", "engadget", "zdnet",
        "infoworld", "javaworld", "thenewstack", "interrupt", "embedded",
    ]),
    ("个人博客/研究者主页", [
        "github.io", "homepages", "homepage", "personal", "people.", "users.",
        "staff.", "~", "about.me", "blogspot", "wordpress.com", "weebly",
        "notion.site", "hey.com", "linktr.ee", "carrd.co", "neocities",
        "vercel.app", "netlify.app", "pages.dev", "glitch.me", "repl.co",
        "surge.sh", "fleek", "ipfs.io", "dweb",
    ]),
    ("素材/图库", [
        "stock photo", "photos", "images", "icons", "fonts", "clipart",
        "vector", "illustration", "mockup", "template", "wallpaper", "unsplash",
        "pexels", "pixabay", "freepik", "flaticon", "iconfinder", "noun project",
        "vecteezy", "shutterstock", "gettyimages", "istockphoto", "adobe stock",
        "rawpixel", "burst.shopify", "kaboompics", "stocksnap", "picjumbo",
        "png", "svg", "logos", "brand", "colors", "palette", "font",
    ]),
    ("应用库/软件下载站", [
        "apk", "download.com", "softpedia", "filehippo", "majorgeeks",
        "softonic", "uptodown", "apkpure", "apkcombo", "apkhere", "appgallery",
        "f-droid", "fdroid", "appstore", "play.google", "microsoft store",
        "winget", "chocolatey", "scoop", "homebrew", "ninite", "portableapps",
        "download", "软件下载", "下载站", "软件库",
    ]),
    ("在线工具/服务", [
        "tool", "tools", "converter", "generator", "editor", "formatter",
        "calculator", "checker", "validator", "formatter", "beautifier",
        "minifier", "compressor", "optimizer", "analyzer", "inspector",
        "debugger", "playground", "sandbox", "fiddle", "codepen", "jsfiddle",
        "codesandbox", "stackblitz", "replit", "colab", "jupyter", "notebook",
        "online", "web app", "free online", "converter", "utility",
    ]),
    ("社区/论坛", [
        "forum", "forums", "community", "discourse", "reddit", "stackexchange",
        "stackoverflow", "quora", "discord", "slack", "telegram", "irc",
        "matrix.to", "gitter", "zulip", "mattermost", "rocketchat", "flarum",
        "nodebb", "phpbb", "mybb", "vanillaforums", "invision", "vbulletin",
        "question", "answer", "ask.", "answers.", "讨论", "问答",
    ]),
    ("目录/索引站", [
        "directory", "catalog", "listing", "index of", "curated", "awesome",
        "collection", "list of", "resources", "links", "linkdump", "bookmarks",
        "nav", "导航", "目录", "收录", "startpage", "start.me", "raindrop",
    ]),
    ("商业服务/营销推广", [
        "saas", "crm", "erp", "marketing", "sales", "pricing", "enterprise",
        "demo", "trial", "signup", "get started", "contact sales", "book a call",
        "roi", "leads", "growth", "agency", "consulting", "agency", "studio",
        "shop", "store", "ecommerce", "e-commerce", "cart", "checkout",
        "shopify", "woocommerce", "magento", "bigcommerce", "squarespace",
        "wix", "webflow", "wordpress.com/business",
    ]),
    ("会议/学术组织", [
        "conference", "workshop", "symposium", "summit", "congress", "expo",
        "neurips", "icml", "iclr", "cvpr", "iccv", "eccv", "acl", "emnlp",
        "naacl", "ijcai", "aaai", "kdd", "siggraph", "sigir", "www conference",
        "chi", "uist", "cscw", "call for papers", "proceedings", "cfp",
        "registration", "attend", "sponsor", "keynote",
    ]),
    ("播客", [
        "podcast", "episode", "listen", "audio show", "radio", "spotify",
        "apple podcast", "anchor.fm", "transistor", "buzzsprout", "libsyn",
    ]),
    ("平台/大厂服务（非资源站）", [
        "google.com", "youtube.com", "youtu.be", "facebook.com", "instagram.com",
        "twitter.com", "x.com", "linkedin.com", "tiktok.com", "douyin.com",
        "bilibili.com", "weibo.com", "zhihu.com", "douban.com", "baidu.com",
        "taobao.com", "jd.com", "pinduoduo.com", "alipay.com", "apple.com",
        "microsoft.com", "amazon.com", "netflix.com", "spotify.com",
        "twitch.tv", "pinterest.com", "snapchat.com", "whatsapp.com",
        "qq.com", "163.com", "126.com", "sina.com", "sohu.com", "ifeng.com",
        "aliyun.com", "tencent.com", "bytedance.com", "meituan.com",
    ]),
]

# TLD 结构启发式：(类别, [TLD 后缀])——整域以它结尾才算
TLD_RULES = [
    ("高校/实验室", [".edu", ".edu.cn", ".edu.au", ".edu.hk", ".ac.cn", ".ac.uk", ".ac.jp", ".ac.kr"]),
    ("标准/联盟/公益组织", [".gov", ".gov.cn", ".gov.uk", ".gov.au", ".mil"]),
    ("AI工具/平台", [".ai"]),
    ("在线工具/服务", [".app", ".dev", ".tools", ".software", ".codes", ".how", ".fyi", ".page", ".site", ".online", ".io"]),
    ("商业服务/营销推广", [".shop", ".store", ".biz", ".agency", ".consulting", ".ventures"]),
    ("技术媒体/实战教程站", [".news", ".blog", ".press", ".media", ".report"]),
]

# 域名里的功能词素 → 类别（弱一些，两处命中才算）
MORPH = [
    ("在线工具/服务", ["tool", "convert", "generate", "calc", "check", "util", "formatter", "editor", "maker", "builder", "finder", "search"]),
    ("电子书/图书", ["book", "lib", "read", "novel"]),
    ("游戏/游戏资源", ["game", "play", "arcade", "quest", "craft"]),
    ("技术媒体/实战教程站", ["tech", "dev", "code", "geek", "byte", "stack", "tips", "learn"]),
    ("商业服务/营销推广", ["shop", "buy", "deal", "sale", "store", "brand", "biz", "market"]),
    ("加密/Web3", ["coin", "chain", "token", "swap", "defi", "nft", "stake"]),
]

TLD_RE = re.compile(r"\.(com|net|org|io|ai|app|dev|cn|co|me|info|xyz|cc|top|site|online|tech|store|blog|news|page|site|wiki|edu|gov|ac)\b")


def _tld(dom):
    i = dom.rfind(".")
    return dom[i:] if i > 0 else ""


def lex_classify(dom):
    """返回 (类别, basis) 或 (None, None)。优先级：词典 > TLD > 词素。"""
    d = dom.lower()
    for cat, words in LEX:
        for w in words:
            if w in d:
                return cat, "ai-lex:" + w
    t = _tld(d)
    for cat, suffixes in TLD_RULES:
        for s in suffixes:
            if d.endswith(s):
                return cat, "ai-tld:" + s
    hits = Counter()
    for cat, morphs in MORPH:
        for m in morphs:
            if m in d:
                hits[cat] += 1
    if hits:
        cat, n = hits.most_common(1)[0]
        if n >= 1:
            return cat, "ai-morph(%d)" % n
    return None, None


def main():
    res = json.load(open(RES, encoding="utf-8"))
    prev = {x["domain"]: x for x in json.load(open(CLS, encoding="utf-8"))} if os.path.exists(CLS) else {}
    manual = json.load(open(MANUAL_JSON, encoding="utf-8")) if os.path.exists(MANUAL_JSON) else {}

    if "--merge" in sys.argv:
        n = 0
        for x in res:
            dom = x["domain"]
            m = manual.get(dom)
            if m and (dom not in prev or prev[dom].get("basis", "").startswith(("ai-",))):
                prev[dom] = {
                    "domain": dom, "url": x.get("url", ""), "found_via": x.get("found_via", ""),
                    "title": prev.get(dom, {}).get("title", ""), "desc": "",
                    "http_ok": True, "err": "", "category": m, "basis": "ai-manual",
                }
                n += 1
        store.atomic_write_json(CLS, sorted(prev.values(), key=lambda y: y["domain"]), indent=2)
        print("并入 %d 条人工判断，classified.json 共 %d 条" % (n, len(prev)))
        return

    todo = [x["domain"] for x in res if x["domain"] not in prev]
    labeled, remain = 0, []
    for dom in todo:
        m = manual.get(dom)
        if m:
            prev[dom] = {"domain": dom, "url": next(x["url"] for x in res if x["domain"] == dom),
                         "found_via": "", "title": "", "desc": "", "http_ok": True, "err": "",
                         "category": m, "basis": "ai-manual"}
            labeled += 1
            continue
        cat, basis = lex_classify(dom)
        if cat:
            prev[dom] = {"domain": dom, "url": next(x["url"] for x in res if x["domain"] == dom),
                         "found_via": "", "title": "", "desc": "", "http_ok": True, "err": "",
                         "category": cat, "basis": basis}
            labeled += 1
        else:
            remain.append(dom)
    store.atomic_write_json(CLS, sorted(prev.values(), key=lambda y: y["domain"]), indent=2)
    store.atomic_write_text(PENDING_TXT, chr(10).join(remain))
    c = Counter(x["category"] for x in prev.values())
    print("本轮词法+人工命中 %d（其中人工 %d），仍待判 %d → %s" % (labeled, len(manual), len(remain), PENDING_TXT))
    print("classified.json 累计 %d 条，分布 top20：" % len(prev))
    for k, v in c.most_common(20):
        print("  %6d  %s" % (v, k))


if __name__ == "__main__":
    main()
