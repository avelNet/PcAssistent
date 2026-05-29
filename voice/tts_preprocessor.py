"""
tts_preprocessor.py — подготовка текста для Silero TTS.

Силеро не умеет произносить английские слова в русском тексте.
Препроцессор конвертирует их в русскую фонетику до передачи в движок.
"""

import re

# ─── Числа → слова (Silero нестабильно читает цифры в русском тексте) ─────────

_ONES = [
    "", "один", "два", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять", "десять",
    "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]
_TENS = [
    "", "", "двадцать", "тридцать", "сорок", "пятьдесят",
    "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
]
_HUNDREDS = [
    "", "сто", "двести", "триста", "четыреста", "пятьсот",
    "шестьсот", "семьсот", "восемьсот", "девятьсот",
]


def num_to_words(n: int) -> str:
    """Целое число 0–999 → русские слова."""
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + num_to_words(-n)
    parts = []
    if n >= 100:
        parts.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10])
        n %= 10
    if n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def _replace_numbers(text: str) -> str:
    """
    Заменить целые числа на русские слова.
    Десятичные (2.5, 3,14) и числа в версиях (v3, llama-3.3) — не трогаем.
    """
    # Сначала матчим десятичные — пропускаем их без замены
    # Потом матчим целые — конвертируем
    def _sub(m: re.Match) -> str:
        s = m.group(0)
        if re.fullmatch(r"\d+[.,]\d+", s):
            return s  # десятичное число — оставляем
        return num_to_words(int(s))

    return re.sub(r"\d+[.,]\d+|\b\d+\b", _sub, text)


# ─── Словарь технических терминов ────────────────────────────────────────────
# Часто встречающиеся слова, которые должны звучать по-русски.
# CamelCase и регистр учитываются отдельно — сравниваем по lower().

_DICT: dict[str, str] = {
    # git
    "git": "гит", "github": "гитхаб", "commit": "коммит", "commits": "коммиты",
    "push": "пуш", "pull": "пул", "merge": "мёрж", "branch": "ветка",
    "rebase": "ребейс", "stash": "стэш", "clone": "клон", "fork": "форк",
    "main": "мейн", "master": "мастер", "dev": "дев", "develop": "девелоп",
    "feature": "фичер", "hotfix": "хотфикс", "release": "релиз",
    "origin": "ориджин", "remote": "ремоут", "upstream": "апстрим",
    # code
    "debug": "дебаг", "config": "конфиг", "log": "лог", "logs": "логи",
    "test": "тест", "tests": "тесты", "build": "билд", "deploy": "деплой",
    "setup": "сетап", "init": "инит", "install": "инсталл", "run": "ран",
    "start": "старт", "stop": "стоп", "restart": "рестарт",
    "fix": "фикс", "patch": "патч", "refactor": "рефактор", "update": "апдейт",
    "api": "эй-пи-ай", "url": "урл", "uri": "ю-эр-ай",
    "json": "джейсон", "yaml": "ямл", "toml": "томл", "xml": "иксэмэль",
    "html": "эйч-ти-эм-эль", "css": "сисиэс", "js": "джей-эс",
    "sql": "эс-ку-эль", "db": "дэбэ", "orm": "о-эр-эм",
    "env": "энв", "venv": "вэнв", "docker": "докер", "linux": "линукс",
    "ubuntu": "убунту", "python": "питон", "bash": "баш", "shell": "шелл",
    "error": "эррор", "warning": "ворнинг", "info": "инфо", "trace": "трейс",
    "import": "импорт", "export": "экспорт", "module": "модуль",
    "class": "класс", "function": "функция", "method": "метод",
    "return": "ретёрн", "type": "тайп", "list": "лист", "dict": "дикт",
    "true": "тру", "false": "фолс", "none": "нан", "null": "нал",
    # exceptions
    "typeerror": "тайп-эррор", "valueerror": "вэлью-эррор",
    "keyerror": "кей-эррор", "attributeerror": "аттрибьют-эррор",
    "indexerror": "индекс-эррор", "runtimeerror": "рантайм-эррор",
    "exception": "эксэпшн", "traceback": "трейсбэк",
    # tools / libs
    "ollama": "олама", "groq": "грок", "obsidian": "обсидиан",
    "jetbrains": "джет брейнс", "vscode": "вискод", "pycharm": "пайчарм",
    "intellij": "интелли-джей", "rider": "райдер",
    "glib": "джи-либ", "gio": "джи-ио", "gtk": "джи-ти-кей",
    "dbus": "ди-бас", "wayland": "вэйланд", "xorg": "икс-орг",
    "asyncio": "асинк-ио", "aiohttp": "эйо-хттп",
    "fastapi": "фаст-апи", "pydantic": "пайдантик",
    "numpy": "намп-ай", "pandas": "пандас", "torch": "торч",
    # auth / security
    "auth": "аут", "oauth": "о-аут", "jwt": "джей-дабл-ю-ти",
    "ssl": "эс-эс-эль", "tls": "ти-эль-эс", "https": "эйч-ти-ти-пи-эс",
    # units / misc
    "ok": "окей", "pr": "пи-ар", "ci": "си-ай", "cd": "си-ди",
    "repo": "репо", "readme": "ридми", "license": "лайсенс",
    "todo": "туду", "fixme": "фикс-ми",
    "pc": "пи-си", "llm": "эль-эль-эм", "llama": "лама",
    "course": "корс", "get": "гет", "set": "сет", "use": "юз",
    "tap": "тэп", "go": "гоу", "payment": "пеймент", "order": "ордер",
    "service": "сервис", "server": "сервер", "client": "клиент",
    "user": "юзер", "token": "токен", "request": "риквест", "response": "респонс",
    "handler": "хэндлер", "manager": "менеджер", "worker": "воркер",
    "task": "таск", "queue": "кью", "event": "ивент", "hook": "хук",
}

# Буква → русская фонема (для неизвестных слов)
_PHONEMES: dict[str, str] = {
    "a": "а",  "b": "б",  "c": "к",  "d": "д",  "e": "е",
    "f": "ф",  "g": "г",  "h": "х",  "i": "и",  "j": "дж",
    "k": "к",  "l": "л",  "m": "м",  "n": "н",  "o": "о",
    "p": "п",  "q": "к",  "r": "р",  "s": "с",  "t": "т",
    "u": "у",  "v": "в",  "w": "в",  "x": "кс", "y": "й",
    "z": "з",
}

# Двухбуквенные согласные сочетания (только надёжные — без гласных диграфов)
_DIGRAPHS: dict[str, str] = {
    "sh": "ш", "ch": "ч", "ph": "ф", "ck": "к",
    "th": "т", "wh": "в", "wr": "р", "kn": "н",
    "qu": "кв", "ng": "нг",
}


def _transliterate(word: str) -> str:
    """Побуквенная транслитерация одного английского слова."""
    w = word.lower()
    out: list[str] = []
    i = 0
    while i < len(w):
        pair = w[i: i + 2]
        if pair in _DIGRAPHS:
            out.append(_DIGRAPHS[pair])
            i += 2
        else:
            out.append(_PHONEMES.get(w[i], w[i]))
            i += 1
    return "".join(out)


def _split_camel(word: str) -> list[str]:
    """
    Разбить CamelCase/PascalCase на части.
    "PcAssistent" → ["Pc", "Assistent"]
    "getConfig"   → ["get", "Config"]
    "parseURL"    → ["parse", "URL"]
    """
    # Вставляем разделитель перед заглавной буквой, следующей за строчной или цифрой
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", word)
    # Вставляем разделитель перед заглавной буквой перед строчной (внутри аббревиатур: "URLParser")
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    # Вставляем разделитель между буквами и цифрами: "Tap2Go" → "Tap 2 Go"
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", spaced)
    return [p for p in spaced.split(" ") if p]


def _convert_token(token: str) -> str:
    """
    Конвертировать один токен (слово без пробелов) в русскую фонетику.
    Порядок: словарь → CamelCase-разбивка → побуквенно.
    """
    # Прямое совпадение в словаре (case-insensitive)
    low = token.lower()
    if low in _DICT:
        return _DICT[low]

    # Пробуем CamelCase-разбивку
    parts = _split_camel(token)
    if len(parts) > 1:
        converted = []
        for part in parts:
            if part.isdigit():
                converted.append(num_to_words(int(part)))
            else:
                plow = part.lower()
                if plow in _DICT:
                    converted.append(_DICT[plow])
                else:
                    converted.append(_transliterate(part))
        return " ".join(converted)

    # Однословный токен — побуквенно
    return _transliterate(token)


def preprocess_for_tts(text: str) -> str:
    """
    Подготовить текст для Silero:
    - Конвертировать английские слова/имена в русскую фонетику
    - Убрать лишние символы (/, _, ., #) из речевого потока
    """

    def _replace(m: re.Match) -> str:
        token = m.group(0)
        # Убираем расширения файлов: "config.yaml" → словарь отработает оба
        # Обрабатываем как единицу: ищем весь токен целиком, потом части
        # Разбиваем по точкам (foo.bar.baz → ["foo", "bar", "baz"])
        if "." in token:
            sub_parts = token.split(".")
            converted = [_convert_token(p) for p in sub_parts if p]
            # "main.py" → "мейн питон" — расширение часто не нужно в речи
            # Убираем последнюю часть если это расширение (≤4 символа, всё маленькое)
            if len(sub_parts) > 1 and len(sub_parts[-1]) <= 4:
                converted = converted[:-1]
            return " ".join(converted)
        return _convert_token(token)

    # Латинские слова → русская фонетика
    result = re.sub(r"[A-Za-z][A-Za-z0-9_.]*", _replace, text)

    # Служебные символы: слэш, подчёркивание → пробел
    result = result.replace("/", " ").replace("_", " ")

    # Числа → русские слова ("3 задачи" → "три задачи")
    result = _replace_numbers(result)

    # Схлопываем лишние пробелы
    result = re.sub(r" {2,}", " ", result).strip()

    return result
