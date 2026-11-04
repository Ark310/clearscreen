# -*- coding: utf-8 -*-
"""LexisNexis listing reader used by the engine before scoring: a copy of
Documents/BLNS Parse Test/BLNS LexisNexis Parser v2.py without its Excel parts.

One change from the original: the main name ends at "Reference No", "Reason:" or
"Notes:" after ANY whitespace. The original required 2+ spaces, so a listing written
"CO. LTD. Reference No.:41536 ..." kept the reference, reason and notes in the name.

parse_listing(text) -> name, addresses (country, state, city), aliases, dobs, ids ...
rows_for(parsed)    -> every name x DOB x ID x (country, state, city), identical merged:
                       (name, type, country, state, city, dob, id)

The original module notes follow.

BLNS LexisNexis listing parser -- v2
====================================

Same extraction as v1, written into separate columns instead of one labelled field.

Usage
-----
  Double-click "Run BLNS Parser v2.bat" -> pick the input Excel, then where to save.
  or:  python "BLNS LexisNexis Parser v2.py"  INPUT.xlsx  [OUTPUT.xlsx]

Input  (unchanged from v1)
-----
  One LexisNexis listing per row, in column A (another column is detected
  automatically if column A does not hold listings). A header row is skipped.

Output
------
  ID | Name | Type | Country | State | City | DOB | ID | Notes

    ID (first)   source row number + letter: 1a, 1b, 1c ... (aa, ab after z)
    Name         the main name, each alias, and a romanised copy of each
                 non-Latin name -- every one on its own rows
    Type         individual | corporate | blank  (see "Type" below)
    Country / State / City   one address per row
    DOB          one date of birth per row, exactly as LexisNexis writes it
    ID (second)  identification number (passport / national ID) when the
                 listing carries one
    Notes        the ORIGINAL listing text, unchanged, repeated on every row
                 of that listing

  Rows = every name x every DOB x every identification number x every distinct
  (country, state, city). Rows identical in all columns are merged -- e.g. two
  addresses that differ only by street or postal code, which v2 does not output.

Type (entity type), decided per listing, first rule that applies:
  1. The listing's Entity Name is a country  -> blank on every row of it
     (FATF jurisdiction listings, and people whose name reads as a country,
     such as "Hong Kong")
  2. DOB, citizenship, nationality or position present  -> individual
  3. A company word in the name (llc, holdings, trading, bank ...)  -> corporate
  4. Otherwise  -> blank

Non-Latin names keep their original row and gain a romanised row:
  Chinese     pypinyin; surname readings (单 Shan, 曾 Zeng, 解 Xie ...) and compound
              surnames (欧阳 Ouyang ...); given name joined: 李庭銀 -> Li Tingyin
  Japanese    pykakasi for kana and kanji. Kanji readings are ambiguous, so when
              the listing has a kana alias, that reading is used instead
  Korean      Hangul by syllable with the conventional surname (이정은 -> Lee Jeong
              Eun); hanja converted to Hangul first (李銀晶 -> 이은정 -> Lee Eun Jeong)
  Bengali, Punjabi (Gurmukhi), Gujarati, Devanagari
              rule-based: restores the implied vowel, drops it where it is silent
  Farsi, Urdu / Punjabi (Shahmukhi), Arabic
              dictionary of common name words, then letter rules. These scripts
              omit short vowels, so a rule-based reading is approximate
  Chinese telegraph codes ("1313 1347") are kept but never romanised.
  Chinese characters in a Japanese listing are read as Japanese, and in a Korean
  listing as Korean hanja -- never as Mandarin.
"""
import re
import string
import unicodedata
from collections import OrderedDict

# Optional language libraries: the parser still runs without them, it just skips
# the romanised row for the scripts they cover.
try:
    from anyascii import anyascii as _anyascii
except ImportError:                                           # pragma: no cover
    _anyascii = None
try:
    from pypinyin import Style as _PyStyle, lazy_pinyin as _lazy_pinyin
except ImportError:                                           # pragma: no cover
    _lazy_pinyin = None
try:
    import pykakasi as _pykakasi
    _KAKASI = _pykakasi.kakasi()
except Exception:                                             # pragma: no cover
    _KAKASI = None
try:
    import hanja as _hanja
except ImportError:                                           # pragma: no cover
    _hanja = None


EMPTY = {"", "none", "unknown", "n/a", "na", "null", "-"}

ADDRESS_LABELS = ["Address Type", "Street 1", "Street 2", "Street 3", "Street",
                  "Postal Code", "City", "State", "Province", "Country"]
AKA_LABELS = ["Full", "First", "Middle", "Last", "Type", "Category"]
ID_LABELS = ["Passport Number", "Passport No", "Passport", "National Identification Number",
             "National ID Number", "National ID", "Identification Number", "Identification",
             "ID Number", "ID No", "Tax ID", "Registration Number", "SSN"]
EXTRA_LABELS = ["DOB", "Citizenship", "Nationality", "PlaceOfBirth", "Place of Birth",
                "Position", "Company Name", "Gender", "Title"] + ID_LABELS

COMPANY_WORDS = {
    "inc", "ltd", "llc", "sa", "gmbh", "corp", "corporation", "limited", "company", "co",
    "plc", "group", "holdings", "trading", "enterprises", "nv", "spa", "ag", "bv", "sarl",
    "pty", "incorporated", "llp", "lp", "bank", "banking", "bancorp", "trust", "capital",
    "financial", "finance", "savings", "insurance", "assurance", "securities", "asset",
    "assets", "investment", "investments", "partners", "ventures", "fund", "funds",
    "aktiengesellschaft", "gesellschaft", "aktiebolag", "kabushiki", "airline", "airlines",
    "airways", "shipping", "logistics", "energy", "petroleum", "oil", "gas", "mining",
    "steel", "pharma", "pharmaceutical", "technologies", "technology", "systems",
    "solutions", "industries", "manufacturing", "motors", "telecom", "sas", "srl", "ab",
    "oy", "as", "kk", "pte", "bhd", "sdn", "foundation", "association", "institute",
    "university", "ministry", "department", "agency", "authority", "council", "committee",
    "party", "church", "studios", "touring", "services", "international", "consulting",
    # international legal forms (dotted forms are folded first: s.r.o. -> sro)
    "sro", "kft", "zrt", "nyrt", "doo", "dd", "ooo", "oao", "zao", "pao", "ao", "jsc",
    "pjsc", "ojsc", "cjsc", "sal", "sae", "tbk", "pt", "bvba", "kg", "gmbh", "sl", "slu",
    "sau", "ltda", "eireli", "pvt", "aps", "asa", "oyj", "gk", "kk", "spzoo", "sarl", "sprl",
    "cv", "bv", "nv", "se", "lda", "limitada", "sdn", "bhd", "llp", "pllc", "lllp",
}
COMPANY_PHRASES = ["real estate", "sa de cv", "s de rl", "sp z o o", "co ltd", "pty ltd",
                   "private limited", "public limited"]

COUNTRIES = {
    "afghanistan", "albania", "algeria", "andorra", "angola", "antigua and barbuda",
    "argentina", "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain",
    "bangladesh", "barbados", "belarus", "belgium", "belize", "benin", "bhutan", "bolivia",
    "bosnia and herzegovina", "botswana", "brazil", "brunei", "bulgaria", "burkina faso",
    "burundi", "cabo verde", "cambodia", "cameroon", "canada", "central african republic",
    "chad", "chile", "china", "colombia", "comoros", "congo", "costa rica", "cote d'ivoire",
    "croatia", "cuba", "cyprus", "czech republic", "czechia",
    "democratic republic of the congo", "denmark", "djibouti", "dominica",
    "dominican republic", "ecuador", "egypt", "el salvador", "equatorial guinea",
    "eritrea", "estonia", "eswatini", "ethiopia", "fiji", "finland", "france", "gabon",
    "gambia", "georgia", "germany", "ghana", "gibraltar", "greece", "grenada", "guatemala",
    "guinea", "guinea-bissau", "guyana", "haiti", "honduras", "hong kong", "hungary",
    "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel", "italy",
    "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kiribati", "kosovo", "kuwait",
    "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia", "libya",
    "liechtenstein", "lithuania", "luxembourg", "macau", "macao", "madagascar", "malawi",
    "malaysia", "maldives", "mali", "malta", "marshall islands", "mauritania", "mauritius",
    "mexico", "micronesia", "moldova", "monaco", "mongolia", "montenegro", "morocco",
    "mozambique", "myanmar", "burma", "namibia", "nauru", "nepal", "netherlands",
    "new zealand", "nicaragua", "niger", "nigeria", "north korea", "north macedonia",
    "norway", "oman", "pakistan", "palau", "palestine", "panama", "papua new guinea",
    "paraguay", "peru", "philippines", "poland", "portugal", "qatar", "romania", "russia",
    "russian federation", "rwanda", "saint kitts and nevis", "saint lucia",
    "saint vincent and the grenadines", "samoa", "san marino", "sao tome and principe",
    "saudi arabia", "senegal", "serbia", "seychelles", "sierra leone", "singapore",
    "slovakia", "slovenia", "solomon islands", "somalia", "south africa", "south korea",
    "korea republic of", "republic of korea", "south sudan", "spain", "sri lanka", "sudan",
    "suriname", "sweden", "switzerland", "syria", "taiwan", "tajikistan", "tanzania",
    "thailand", "timor-leste", "togo", "tonga", "trinidad and tobago", "tunisia",
    "turkey", "turkiye", "turkmenistan", "tuvalu", "uganda", "ukraine",
    "united arab emirates", "united kingdom", "united states", "uruguay", "uzbekistan",
    "vanuatu", "vatican", "venezuela", "vietnam", "viet nam", "yemen", "zambia",
    "zimbabwe", "cayman islands", "british virgin islands", "bermuda", "isle of man",
    "jersey", "guernsey", "curacao", "aruba", "puerto rico", "greenland", "kurdistan",
    "lao pdr", "lao people's democratic republic", "dprk", "drc", "uae", "usa", "uk",
}


# ------------------------------------------------------------------ helpers --
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                         "“": '"', "”": '"', "„": '"', "′": "'"})
_KEEP_JOINERS = re.compile("[؀-ۿऀ-෿]")     # Arabic-script + Indic


def clean(value):
    """Trim a value and drop LexisNexis placeholders.

    NFKC folds compatibility code points (Korean data stores the hanja for 'Lee' as
    U+F9E1, identical-looking to U+674E). Invisible format characters are removed,
    except the zero-width (non-)joiner inside Indic and Arabic-script text, where it
    changes how a word is written (Persian خامنه‌ای)."""
    v = unicodedata.normalize("NFKC", value or "")
    keep = bool(_KEEP_JOINERS.search(v))
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf" or (keep and ch in "‌‍"))
    v = v.translate(_QUOTES)
    v = re.sub(r"-{3,}", " ", v)
    v = re.sub(r"\s+", " ", v).strip().strip(",;").strip()
    return "" if v.lower() in EMPTY else v


def split_blocks(section):
    return [b for b in re.split(r"-{10,}", section or "") if b.strip()]


def section(text, start, ends):
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = min([text.find(e, i) for e in ends if text.find(e, i) >= 0] or [len(text)])
    return text[i:j]


def labelled(block, labels):
    """'Label: value' pairs; a value runs until the next known label."""
    alt = "|".join(re.escape(l) for l in sorted(labels, key=len, reverse=True))
    rx = re.compile(rf"(?<![A-Za-z])({alt})\s*:\s*(.*?)(?=(?<![A-Za-z])(?:{alt})\s*:|$)", re.S)
    out = OrderedDict()
    for label, value in rx.findall(block):
        out.setdefault(label, []).append(clean(value))
    return out


def first(d, *keys):
    for k in keys:
        for v in d.get(k, []):
            if v:
                return v
    return ""


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        key = x.lower() if isinstance(x, str) else x
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


# ============================================================ romanisation ==
_FARSI_LETTERS = set("پچژگکیۀ")
_URDU_LETTERS = set("ٹڈڑںےھ")


def script_of(text):
    """Dominant writing system of a name."""
    if re.fullmatch(r"\s*\d{4}(\s+\d{4})*\s*", text or ""):
        return "chinese telegraph code"
    counts = {}
    for ch in text:
        if ch.isalpha():
            s = unicodedata.name(ch, "UNKNOWN").split()[0]
            s = {"CJK": "chinese", "HANGUL": "hangul", "HIRAGANA": "japanese",
                 "KATAKANA": "japanese"}.get(s, s.lower())
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return "latin"
    sc = max(counts, key=counts.get)
    if sc == "arabic":
        chars = set(text)
        if chars & _URDU_LETTERS:
            return "urdu"
        if chars & _FARSI_LETTERS:
            return "farsi"
    return sc


# ---- Indic (Devanagari, Bengali, Gurmukhi, Gujarati, ...) --------------------
_INDIC_FIX = {"ca": "cha", "cha": "chha", "ssa": "sha", "nna": "na", "tta": "ta",
              "ttha": "tha", "dda": "da", "ddha": "dha", "yya": "ya", "rra": "ra",
              "rha": "rh", "lla": "la", "llla": "la", "qa": "qa", "khandata": "t"}
_VMAP = {"aa": "a", "ii": "i", "uu": "u", "vocalicr": "ri"}
_VOWEL_LETTERS = {"a", "aa", "i", "ii", "u", "uu", "e", "ai", "o", "au", "vocalicr", "ee", "oo"}
INDIC = {"devanagari", "bengali", "gurmukhi", "gujarati", "oriya", "tamil", "telugu",
         "kannada", "malayalam", "sinhala"}


def _indic(text):
    """Rule-based romanisation of an Indic abugida, from Unicode character names.

    Each consonant carries an inherent 'a' unless a vowel sign or virama follows. The
    inherent vowel is then dropped where it is silent: at the end of a word, and
    between a vowel and a following consonant group (standard schwa deletion):
    माया शर्मा -> maya sharma, ਹਰਪ੍ਰੀਤ ਸਿੰਘ -> harprit singh, পটেল -> patel."""
    words = []
    for word in text.split():
        units, double_next = [], False
        for ch in word:
            name = unicodedata.name(ch, "")
            if " LETTER " in name:
                base = name.split(" LETTER ")[1].lower().replace(" ", "")
                if base in _VOWEL_LETTERS:
                    units.append(["", _VMAP.get(base, base), False, False])
                    continue
                base = _INDIC_FIX.get(base, base)
                stem = base[:-1] if base.endswith("a") and len(base) > 1 else base
                if double_next and stem:
                    stem = stem[0] + stem
                    double_next = False
                units.append([stem, "a", True, False])
            elif "VOWEL SIGN" in name and units:
                v = name.split("VOWEL SIGN ")[1].lower().replace(" ", "")
                units[-1][1], units[-1][2] = _VMAP.get(v, v), False
            elif "VIRAMA" in name and units:
                units[-1][1], units[-1][2], units[-1][3] = "", False, True
            elif any(k in name for k in ("ANUSVARA", "CANDRABINDU", "TIPPI", "BINDI")) and units:
                units[-1][1] += "n"
            elif "VISARGA" in name and units:
                units[-1][1] += "h"
            elif "ADDAK" in name:                    # Gurmukhi: doubles the next consonant
                double_next = True
            elif ch.isascii():
                units.append([ch, "", False, False])
        n = len(units)
        for i in range(n - 1, 0, -1):
            u = units[i]
            if not u[2]:
                continue
            if i == n - 1:
                if not units[i - 1][3]:                  # not after an explicit conjunct
                    u[1] = ""
                continue
            nxt = units[i + 1]
            if units[i - 1][1] != "" and nxt[0]:
                if nxt[1]:                              # C a C V -> drop the a
                    u[1] = ""
                else:                                   # C a C-C V: only before a WRITTEN
                    j = i + 1                           # vowel (हरप्रीत -> harprit); a cluster
                    while j < n and units[j][3]:        # ending in an implied vowel
                        j += 1                          # keeps it (कूपर्स)
                    if j < n and units[j][1] and not units[j][2]:
                        u[1] = ""
        words.append("".join(u[0] + u[1] for u in units))
    return " ".join(w for w in words if w)


# ---- Perso-Arabic (Farsi, Urdu / Shahmukhi, Arabic) --------------------------
# These scripts omit short vowels, so letters alone cannot give 'Mohammad' from
# 'محمد'. Common name words come from this dictionary; anything else falls back to
# the letter rules below and is tagged approximate.
PERSO_ARABIC_NAMES = {
    "محمد": "Mohammad", "محمّد": "Mohammad", "أحمد": "Ahmad", "احمد": "Ahmad",
    "علی": "Ali", "علي": "Ali", "حسن": "Hassan", "حسین": "Hossein", "حسين": "Hussein",
    "رضا": "Reza", "مهدی": "Mehdi", "مهدي": "Mahdi", "فاطمه": "Fatemeh", "فاطمة": "Fatima",
    "زهرا": "Zahra", "مریم": "Maryam", "مريم": "Maryam", "عمر": "Omar", "عثمان": "Osman",
    "ابراهیم": "Ebrahim", "إبراهيم": "Idrafiy", "ابراهيم": "Idrafiy", "یوسف": "Yousef",
    "يوسف": "Yusuf", "خالد": "Khalid", "عمران": "Imran", "عماد": "Imad", "اسامة": "Ocawa",
    "أسامة": "Ocawa", "اسامه": "Ocawa", "عبد": "Abd", "عبدالله": "Abdullah", "الله": "Allah",
    "عبدالرحمن": "Abdulrahman", "عبدالعزيز": "Abdulaziz", "سید": "Seyed", "سيد": "Sayed",
    "بن": "bin", "بنت": "bint", "ابو": "Abu", "أبو": "Abu", "امام": "Imam", "إمام": "Imam",
    "شیخ": "Sheikh", "شيخ": "Sheikh", "خان": "Khan", "بیگ": "Baig", "ملک": "Malik",
    "ملك": "Malik", "شاه": "Shah", "حسینی": "Hosseini", "احمدی": "Ahmadi",
    "محمدی": "Mohammadi", "رضایی": "Rezaei", "کریمی": "Karimi", "موسوی": "Mousavi",
    "رحیمی": "Rahimi", "جعفری": "Jafari", "صادقی": "Sadeghi", "قاسمی": "Ghasemi",
    "خامنه‌ای": "Khamenei", "خامنه‌ای": "Khamenei", "خامنهای": "Khamenei",
    "روحانی": "Rouhani", "ظریف": "Zarif", "رئیسی": "Raisi", "سلیمانی": "Soleimani",
    "تهرانی": "Tehrani", "زاده": "Zadeh", "پور": "Pour", "نژاد": "Nejad", "قریشی": "Qureshi",
    "صدیقی": "Siddiqui", "نواز": "Nawaz", "شریف": "Sharif", "بھٹو": "Bhutto",
    "چوہدری": "Chaudhry", "چودھری": "Chaudhry", "اقبال": "Iqbal", "طارق": "Tariq",
    "عباس": "Abbas", "جاوید": "Javed", "پرویز": "Pervez", "مشرف": "Musharraf",
    "لادن": "Taven", "الخليل": "Al-Khalil", "خليل": "Khalil", "حمزه": "Hamza",
    "حمزة": "Hamza", "مصطفى": "Mustafa", "مصطفی": "Mostafa", "صالح": "Saleh",
    "سلطان": "Sultan", "نور": "Noor", "جمال": "Jamal", "كمال": "Kamal", "کمال": "Kamal",
    "رحمان": "Rahman", "الرحمن": "Al-Rahman", "بیگم": "Begum", "کوثر": "Kausar",
    "جواد": "Javad", "ایران": "Iran", "امیر": "Amir", "علیرضا": "Alireza", "حمید": "Hamid",
    "مجید": "Majid", "سعید": "Saeed", "ناصر": "Naser", "اکبر": "Akbar", "هاشمی": "Hashemi",
    "رفسنجانی": "Rafsanjani", "قاسم": "Ghasem", "محمود": "Mahmoud", "احمدی‌نژاد": "Ahmadinejad",
}
_PA_LETTERS = {
    "ا": "a", "آ": "a", "أ": "a", "إ": "i", "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j",
    "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "'", "غ": "gh",
    "ف": "f", "ق": "q", "ک": "k", "ك": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n",
    "و": "v", "ه": "h", "ة": "a", "ی": "y", "ي": "y", "ى": "a", "ئ": "'", "ؤ": "'", "ء": "'",
    "ٹ": "t", "ڈ": "d", "ڑ": "r", "ں": "n", "ے": "e", "ھ": "h", "ۀ": "eh",
}


def _pa_word(w, script):
    if w in PERSO_ARABIC_NAMES:
        return PERSO_ARABIC_NAMES[w]
    if w.startswith(("ال", "آل")) and len(w) > 3:                  # definite article
        rest = w[2:]
        return "Al-" + (PERSO_ARABIC_NAMES.get(rest) or _pa_word(rest, script))
    out = []
    for i, ch in enumerate(w):
        r = _PA_LETTERS.get(ch)
        if r is None:
            continue
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if i == 0 and ch == "ا" and nxt in "یي":        # initial alef + yeh = long i
            out.append("i")
            continue
        if i == 1 and w[0] == "ا" and ch in "یي":
            continue
        if i == 0 and ch == "ا" and nxt == "و":          # initial alef + waw = long o/u
            out.append("o")
            continue
        if i == 1 and w[0] == "ا" and ch == "و":
            continue
        if i > 0 and ch == "و":                          # waw: consonant 'v' before alef
            r = "v" if nxt in "اآ" else "u"              # (جواد Javad), else long u/o
        elif i > 0 and ch in "یي":                       # yeh: consonant 'y' before alef
            r = "y" if nxt in "اآ" else "i"              # (ایران), else long i
        elif ch == "ه" and i == len(w) - 1 and i > 0:    # final heh
            r = "eh" if script == "farsi" else "a"
        out.append(r)
    return "".join(out).capitalize()


def _perso_arabic(text, script):
    return " ".join(_pa_word(w, script) for w in text.split() if w)


# ---- Chinese / Japanese / Korean -------------------------------------------
# Characters whose reading as a SURNAME differs from their everyday reading.
CHINESE_SURNAME_READINGS = {
    "单": "shan", "單": "shan", "曾": "zeng", "解": "xie", "仇": "qiu", "区": "ou", "區": "ou",
    "查": "zha", "朴": "piao", "盖": "ge", "蓋": "ge", "覃": "qin", "缪": "miao", "繆": "miao",
    "翟": "zhai", "召": "shao", "乐": "yue", "樂": "yue", "秘": "bi", "员": "yun", "員": "yun",
    "种": "chong", "種": "chong", "宿": "su", "洗": "xian", "冼": "xian", "薄": "bo",
    "黑": "he", "牟": "mou", "纪": "ji", "紀": "ji", "华": "hua", "華": "hua", "任": "ren",
    "沈": "shen", "瞿": "qu", "尉": "yu", "长": "chang", "長": "chang", "万": "wan",
    "萬": "wan", "都": "du", "藏": "zang", "阚": "kan", "闞": "kan", "繁": "po", "过": "guo",
    "過": "guo", "句": "gou", "能": "nai", "邪": "ye", "重": "chong", "石": "shi", "柏": "bai",
}
CHINESE_COMPOUND_SURNAMES = {
    "欧阳": "Ouyang", "歐陽": "Ouyang", "司马": "Sima", "司馬": "Sima", "诸葛": "Zhuge",
    "諸葛": "Zhuge", "上官": "Shangguan", "东方": "Dongfang", "東方": "Dongfang",
    "皇甫": "Huangfu", "令狐": "Linghu", "慕容": "Murong", "夏侯": "Xiahou",
    "尉迟": "Yuchi", "尉遲": "Yuchi", "公孙": "Gongsun", "公孫": "Gongsun",
    "长孙": "Zhangsun", "長孫": "Zhangsun", "司徒": "Situ", "端木": "Duanmu",
    "独孤": "Dugu", "獨孤": "Dugu", "南宫": "Nangong", "南宮": "Nangong", "宇文": "Yuwen",
    "轩辕": "Xuanyuan", "軒轅": "Xuanyuan", "单于": "Chanyu", "單于": "Chanyu",
}


def _chinese(text):
    """Surname + joined given name, Chinese convention: 李庭銀 -> Li Tingyin."""
    if _lazy_pinyin is None:
        return "", ""
    parts = []
    for w in text.split():
        sur = next((k for k in CHINESE_COMPOUND_SURNAMES if w.startswith(k)), None)
        if sur:
            surname, given = CHINESE_COMPOUND_SURNAMES[sur], w[len(sur):]
        else:
            surname = (CHINESE_SURNAME_READINGS.get(w[0])
                       or "".join(_lazy_pinyin(w[0], style=_PyStyle.NORMAL))).capitalize()
            given = w[1:]
        g = "".join(_lazy_pinyin(given, style=_PyStyle.NORMAL)).capitalize() if given else ""
        parts.append(" ".join(x for x in (surname, g) if x))
    return " ".join(parts), "pinyin, surname reading"


def _japanese(text):
    """Japanese kana or kanji -> romaji, word by word, via pykakasi."""
    if _KAKASI is None:
        return ""
    words = []
    for w in text.replace("・", " ").split():
        words.append("".join(r["hepburn"] for r in _KAKASI.convert(w)).capitalize())
    return " ".join(x for x in words if x)


KOREAN_SURNAMES = {
    "이": "Lee", "리": "Ri", "김": "Kim", "박": "Park", "최": "Choi", "정": "Jung",
    "강": "Kang", "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim", "한": "Han",
    "오": "Oh", "신": "Shin", "서": "Seo", "권": "Kwon", "황": "Hwang", "안": "Ahn",
    "송": "Song", "류": "Ryu", "유": "Yoo", "홍": "Hong", "전": "Jeon", "고": "Ko",
    "문": "Moon", "양": "Yang", "손": "Son", "배": "Bae", "백": "Baek", "허": "Heo",
    "노": "Noh", "남": "Nam", "심": "Shim", "곽": "Kwak", "성": "Sung", "차": "Cha",
    "주": "Joo", "우": "Woo", "구": "Koo", "민": "Min", "나": "Na", "진": "Jin",
    "지": "Ji", "엄": "Um", "변": "Byun", "채": "Chae", "원": "Won", "천": "Chun",
    "방": "Bang", "공": "Kong", "현": "Hyun", "함": "Ham", "여": "Yeo", "추": "Choo",
    "도": "Do", "석": "Seok", "선": "Sun", "설": "Seol", "마": "Ma", "길": "Gil",
    "연": "Yeon", "표": "Pyo", "명": "Myung", "기": "Ki", "반": "Ban", "왕": "Wang",
    "금": "Keum", "옥": "Ok", "인": "In", "맹": "Maeng", "모": "Mo", "탁": "Tak",
    "국": "Kook", "용": "Yong", "경": "Kyung", "봉": "Bong",
    "황보": "Hwangbo", "남궁": "Namgung", "선우": "Sunwoo", "제갈": "Jegal", "독고": "Dokgo",
}
_TWO_SYLLABLE_SURNAMES = ("황보", "남궁", "선우", "제갈", "독고")


def _hangul(text):
    """Syllable by syllable, surname in its conventional spelling: 이정은 -> Lee Jeong Eun."""
    if _anyascii is None:
        return ""
    words = []
    for w in text.split():
        sur = next((k for k in _TWO_SYLLABLE_SURNAMES if w.startswith(k)), w[:1])
        rest = w[len(sur):]
        if sur in KOREAN_SURNAMES and rest:
            words.append(KOREAN_SURNAMES[sur])
            words += [_anyascii(c).capitalize() for c in rest]
        else:
            words += [_anyascii(c).capitalize() for c in w]
    return " ".join(x for x in words if x)


JAPAN = {"japan"}
KOREA = {"south korea", "north korea", "korea republic of", "republic of korea", "korea"}


def romanise(text, script, ctx):
    """Romanised form of a non-Latin name, or '' when it cannot be done reliably.

    ctx = {"countries": {...}, "scripts": {...}} for the listing. Chinese characters
    are read as Japanese in a Japanese listing and as Korean hanja in a Korean one."""
    if script in INDIC:
        return _indic(text).title()
    if script in ("farsi", "urdu", "arabic"):
        return _perso_arabic(text, script)
    if script == "hangul":
        return _hangul(text)
    if script == "japanese":
        return _japanese(text)
    if script == "chinese":
        if "japanese" in ctx["scripts"] or ctx["countries"] & JAPAN:
            # kanji readings are ambiguous; a kana alias in the listing is authoritative
            return "" if "japanese" in ctx["scripts"] else _japanese(text)
        if "hangul" in ctx["scripts"] or ctx["countries"] & KOREA:
            if _hanja is None:
                return ""
            return _hangul(_hanja.translate(text, "substitution"))
        return _chinese(text)[0]
    if _anyascii is None:
        return ""
    r = re.sub(r"[^\w\s'\-.]", " ", _anyascii(text))
    return re.sub(r"\s+", " ", r).strip()


# =================================================================== parse ==
def parse_listing(text):
    t = text or ""
    head = t[:t.find("Address Information")] if "Address Information" in t else t
    name = clean((re.search(r"Name\s*:\s*(.*?)(?=\s+Reference No|\s+Reason\s*:|\s+Notes\s*:|$)",
                            head, re.S) or [None, ""])[1])

    addresses = []
    for block in split_blocks(section(t, "Address Information", ["AKA Listing", "Additional Information"])):
        d = labelled(block, ADDRESS_LABELS)
        geo = (first(d, "Country"), first(d, "State", "Province"), first(d, "City"))
        if any(geo):
            addresses.append(geo)
    addresses = dedupe(addresses)

    aliases = []
    for block in split_blocks(section(t, "AKA Listing", ["Additional Information"])):
        d = labelled(block, AKA_LABELS)
        full = first(d, "Full") or " ".join(x for x in (first(d, "First"), first(d, "Middle"),
                                                         first(d, "Last")) if x)
        if full and full.lower() != name.lower():
            aliases.append(full)
    aliases = dedupe(aliases)

    extras = OrderedDict()
    for block in split_blocks(section(t, "Additional Information", [])):
        for k, vs in labelled(block, EXTRA_LABELS).items():
            extras.setdefault(k, []).extend(vs)
    vals = lambda *ks: dedupe([v for k in ks for v in extras.get(k, []) if v])   # noqa: E731

    return dict(name=name, addresses=addresses, aliases=aliases,
                dobs=vals("DOB"), ids=vals(*ID_LABELS),
                citizenship=vals("Citizenship"), nationality=vals("Nationality"),
                position=vals("Position"), company=vals("Company Name"),
                recognised=bool(name))


def is_company_name(name):
    """A legal form or company word in the name. Dotted forms are folded first,
    so 'S.A.S.' -> 'sas' and 's.r.o.' -> 'sro'."""
    low = re.sub(r"\b(?:[a-z]\.){2,}(?:[a-z]\b)?", lambda m: m.group(0).replace(".", ""),
                 unicodedata.normalize("NFKC", name or "").lower())
    flat = " " + re.sub(r"[^a-z0-9]+", " ", low).strip() + " "
    return bool(set(flat.split()) & COMPANY_WORDS
                or any(f" {ph} " in flat for ph in COMPANY_PHRASES))


def entity_type(p):
    """Listing-level type, first rule that applies (see module docstring).

    LexisNexis uses 'Position:' for companies too -- as a description of the
    listing ('Entity listed on the FinCEN US MSB list'), not a job title -- so a
    Position alone ranks BELOW a company word."""
    if p["name"].lower().strip() in COUNTRIES:
        return ""
    if p["dobs"] or p["citizenship"] or p["nationality"]:
        return "individual"
    if is_company_name(p["name"]) or p["company"]:
        return "corporate"
    if p["position"]:
        return "individual"
    return ""


def names_of(p):
    """Main name, aliases, and a romanised copy of every non-Latin name."""
    ctx = {"countries": {a[0].lower() for a in p["addresses"]}
           | {c.lower() for c in p["citizenship"] + p["nationality"]},
           "scripts": {script_of(n) for n in [p["name"]] + p["aliases"]}}
    out = [p["name"]]
    for n in [p["name"]] + p["aliases"]:
        if n != p["name"]:
            out.append(n)
        sc = script_of(n)
        if sc not in ("latin", "chinese telegraph code"):
            r = romanise(n, sc, ctx)
            if r:
                out.append(r)
    return dedupe(out)


def rows_for(p):
    """Every name x DOB x identification x (country, state, city); identical rows merged."""
    etype = entity_type(p)
    rows = []
    for name in names_of(p):
        for dob in p["dobs"] or [""]:
            for ident in p["ids"] or [""]:
                for country, state, city in p["addresses"] or [("", "", "")]:
                    rows.append((name, etype, country, state, city, dob, ident))
    return dedupe(rows)


def suffix(i):
    """0 -> a, 25 -> z, 26 -> aa ..."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = string.ascii_lowercase[r] + s
    return s
