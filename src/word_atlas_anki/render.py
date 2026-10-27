import html
import re

from .constants import FIELDS, PROJECT_REPO, UPSTREAM_REPO


def escape(value):
    # Square brackets must not let source text inject Anki sound/LaTeX directives.
    return html.escape(value, quote=True).replace("[", "&#91;").replace("]", "&#93;")


IRREGULAR = dict(row.split(":", 1) for row in (
    "be:am is are was were been being|have:has had having|do:does did done doing|go:goes went gone going|"
    "buy:bought|bring:brought|catch:caught|teach:taught|think:thought|seek:sought|fight:fought|"
    "take:took taken taking|give:gave given giving|make:made making|come:came coming|become:became becoming|"
    "see:saw seen seeing|eat:ate eaten eating|drink:drank drunk drinking|write:wrote written writing|"
    "read:read reading|speak:spoke spoken speaking|break:broke broken breaking|choose:chose chosen choosing|"
    "know:knew known knowing|grow:grew grown growing|throw:threw thrown throwing|draw:drew drawn drawing|"
    "fly:flew flown flies flying|run:ran running|begin:began begun beginning|swim:swam swum swimming|"
    "sit:sat sitting|stand:stood standing|find:found finding|feel:felt feeling|keep:kept keeping|"
    "leave:left leaving|lose:lost losing|meet:met meeting|pay:paid paying|say:said saying|tell:told telling|"
    "sell:sold selling|hear:heard hearing|hold:held holding|get:got gotten getting|send:sent sending|"
    "spend:spent spending|build:built building|sleep:slept sleeping|fall:fell fallen falling|"
    "rise:rose risen rising|ride:rode ridden riding|drive:drove driven driving|wear:wore worn wearing|"
    "forget:forgot forgotten forgetting|understand:understood understanding|cut:cut cutting|put:put putting|"
    "let:let letting|set:set setting|hit:hit hitting|win:won winning|child:children|person:people|"
    "man:men|woman:women|foot:feet|tooth:teeth|mouse:mice"
).split("|"))


def highlight(sentence, word):
    base = word.casefold().strip()
    forms = {base}
    if re.fullmatch(r"[a-z]+", base):
        if base in IRREGULAR:
            forms.update(IRREGULAR[base].split())
        forms.add(base + "s")
        if base.endswith(("s", "sh", "ch", "x", "z", "o")):
            forms.add(base + "es")
        if len(base) > 2 and base.endswith("y") and base[-2] not in "aeiou":
            forms.update((base[:-1] + "ies", base[:-1] + "ied"))
        if len(base) > 2:
            forms.update((base + "ed", base + "ing"))
            if base.endswith("e"):
                forms.update((base + "d", base[:-1] + "ing"))
            if re.search(r"[^aeiou][aeiou][bdgmnprt]$", base):
                forms.update((base + base[-1] + "ed", base + base[-1] + "ing"))
    pattern = r"(?<![A-Za-z])(?:" + "|".join(re.escape(form).replace(r"\ ", r"\s+") for form in sorted(forms, key=lambda f: (-len(f), f))) + r")(?![A-Za-z])"
    match = re.search(pattern, sentence, re.IGNORECASE)
    if not match:
        return escape(sentence)
    return escape(sentence[:match.start()]) + "<mark>" + escape(match.group()) + "</mark>" + escape(sentence[match.end():])


SOURCE = (
    f'<a href="{UPSTREAM_REPO}">Word Atlas contributors</a> '
    '| <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. '
    f'<a href="{PROJECT_REPO}">非官方 Anki 适配</a>：重新排版、双音标与离线媒体；未重生成文本、插画或录音。'
    'AI 生成内容，未经逐条人工审校。字体 Fraunces (OFL 1.1)，图标 Lucide (ISC)。'
)


def render_fields(entry, canonical_id, topic_label, media):
    fields = {name: "" for name in FIELDS}
    fields.update(AtlasID=escape(canonical_id), Word=escape(entry["w"]), IPAUK=escape(entry["uk"]),
                  IPAUS=escape(entry["us"]), CEFR=entry["cefr"],
                  Topic=escape(f'{topic_label} ({entry["topic"]})'), Tip=escape(entry["tip"]),
                  UsageNote=escape(entry["note"]), Source=SOURCE)
    fields["Senses"] = '<ol class="wa-senses">' + "".join(
        f'<li><span class="wa-pos" data-pos="{escape(s["pos"])}">{escape(s["pos"])}</span>'
        f'<span class="wa-meaning">{escape(s["zh"])}</span>'
        f'<span class="wa-definition" lang="en">{escape(s["en"])}</span></li>' for s in entry["senses"]
    ) + "</ol>"
    for i, example in enumerate(entry["ex"], 1):
        fields[f"Example{i}"] = highlight(example["en"], entry["w"])
        fields[f"Translation{i}"] = escape(example["zh"])
    if "img" in media:
        fields["Image"] = f'<img class="wa-illustration" src="{media["img"]["name"]}" alt="{escape(entry["w"])}" width="512" height="512">'
    for kind, field in (("uk", "AudioUK"), ("us", "AudioUS"), ("ex", "AudioExample1"), ("ex2", "AudioExample2")):
        if kind in media:
            name = media[kind]["name"]
            fields[field] = (f'[sound:{name}]' if kind in ("uk", "ex") else
                             f'<audio class="wa-manual-audio" controls preload="none" src="{name}"></audio>')
    if entry["col"]:
        pairs = [re.split(r"[|\uff5c]", value, maxsplit=1) for value in entry["col"]]
        fields["Collocations"] = '<ul class="wa-collocations">' + "".join(
            f'<li><span lang="en">{escape(pair[0].strip())}</span>' +
            (f'<small>{escape(pair[1].strip())}</small>' if len(pair) == 2 else "") + "</li>" for pair in pairs) + "</ul>"
    for source, field in (("fam", "Family"), ("syn", "Synonyms"), ("ant", "Antonyms")):
        fields[field] = "".join(f'<span class="wa-term">{escape(v)}</span>' for v in entry[source])
    return fields
