from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_REPO = "https://github.com/designfuchen/word-atlas"
PROJECT_REPO = "https://github.com/Misakamoe/WordAtlas-Anki"
DECK_DESCRIPTION = (
    '<div style="text-align: center;">'
    f'<a href="{UPSTREAM_REPO}">Word Atlas</a> | 非官方 '
    f'<a href="{PROJECT_REPO}">Anki 适配</a>：重新排版、双音标与离线媒体；'
    '未重生成文本、插画或录音。AI 生成内容，未经逐条人工审校。'
    '</div>'
)
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
SCHEMA_VERSION = 1
MODEL_ID = 1658245818
ROOT_DECK_ID = 2070907428
DECK_IDS = dict(zip(LEVELS, (1403884763, 1185388194, 1444595532, 1107746864, 2075237730, 1728942772)))
TEMPLATE_ID = 1905831641
FIELDS = (
    "AtlasID", "Word", "IPAUK", "IPAUS", "CEFR", "Topic", "Image", "Senses",
    "Example1", "Translation1", "Example2", "Translation2", "AudioUK", "AudioUS",
    "AudioExample1", "AudioExample2", "Collocations", "Family", "Synonyms",
    "Antonyms", "Tip", "UsageNote", "Source",
)
FIELD_IDS = (
    1786115925, 1409966273, 1722613914, 1454707395, 1808222656, 1884764769,
    1305472875, 1703063772, 1355915841, 2106236469, 1659241649, 1340135086,
    1333471189, 1912994102, 1796362613, 1737118252, 1939939537, 1087140108,
    1518241530, 1160114650, 2080946363, 1753333988, 1514415667,
)
MEDIA_BITS = {"img": 1, "uk": 2, "us": 4, "ex": 8, "ex2": 16}
MAX_PACKAGE_BYTES = int(1.9 * 1024**3)
LICENSE_PATHS = ("LICENSE", "CONTENT_LICENSE.md")
