"""v6.2: the devices a reader reads on, chosen once on the start page (home.<domain>).

What each choice changes:
  - a Kobo: its comics get a Kobo copy made for its screen (KCC's profile for that model), in
    colour only for a colour Kobo; one Kobo copy is shared by everyone who has the comic, so it is
    made for the sharpest screen among them, in colour when any of them reads in colour;
  - a Kindle: a comic sent to it is converted for that model (a black-and-white Kindle gets a
    greyscale copy: smaller, so fewer mail parts);
  - a phone or tablet: nothing is converted (reading apps take the CBZ and EPUB as they are); the
    start page and the guides name the apps for it.
A reader who has chosen nothing gets exactly what v6.1 did: the configured profiles, in colour."""
import config

KOBO, KINDLE, PHONE, TABLET = "kobo", "kindle", "phone", "tablet"
COLOUR_PROFILES = {"KoLC", "KoCC", "KCS", "KSCS"}

# key, family, name, KCC profile (None: the configured default), platform
MODELS = [
    ("kobo-libra-colour", KOBO, "Kobo Libra Colour", "KoLC", None),
    ("kobo-clara-colour", KOBO, "Kobo Clara Colour", "KoCC", None),
    ("kobo-clara-bw", KOBO, "Kobo Clara BW / Clara 2E / Clara HD", "KoC", None),
    ("kobo-libra-2", KOBO, "Kobo Libra 2 / Libra H2O", "KoL", None),
    ("kobo-sage", KOBO, "Kobo Sage", "KoS", None),
    ("kobo-elipsa", KOBO, "Kobo Elipsa / Elipsa 2E", "KoE", None),
    ("kobo-forma", KOBO, "Kobo Forma", "KoF", None),
    ("kobo-nia", KOBO, "Kobo Nia", "KoN", None),
    ("kobo-other", KOBO, "Another Kobo", None, None),
    ("kindle-colorsoft", KINDLE, "Kindle Colorsoft", "KCS", None),
    ("kindle-paperwhite-2024", KINDLE, "Kindle Paperwhite (2024)", "KPW6", None),
    ("kindle-paperwhite-2021", KINDLE, "Kindle Paperwhite (2021) / Signature Edition", "KPW5", None),
    ("kindle-paperwhite-old", KINDLE, "Kindle Paperwhite (2015-2018)", "KPW34", None),
    ("kindle-basic", KINDLE, "Kindle (2022 / 2024)", "K11", None),
    ("kindle-oasis", KINDLE, "Kindle Oasis", "KO", None),
    ("kindle-scribe", KINDLE, "Kindle Scribe (2022 / 2024)", "KS", None),
    ("kindle-scribe-3", KINDLE, "Kindle Scribe (2025)", "KS3", None),
    ("kindle-scribe-colorsoft", KINDLE, "Kindle Scribe Colorsoft", "KSCS", None),
    ("kindle-other", KINDLE, "Another Kindle", None, None),
    ("iphone", PHONE, "iPhone", None, "ios"),
    ("android-phone", PHONE, "Android phone", None, "android"),
    ("ipad", TABLET, "iPad", None, "ios"),
    ("android-tablet", TABLET, "Android tablet", None, "android"),
]
BY_KEY = {m[0]: {"key": m[0], "family": m[1], "name": m[2], "profile": m[3], "platform": m[4]} for m in MODELS}
FAMILIES = [(KOBO, "Kobo"), (KINDLE, "Kindle"), (PHONE, "Phone"), (TABLET, "Tablet")]

# KCC's screen sizes for the profiles above (kindlecomicconverter/image.py, KCC v12)
SCREEN = {"KoLC": (1264, 1680), "KoCC": (1072, 1448), "KoC": (1072, 1448), "KoL": (1264, 1680),
          "KoS": (1440, 1920), "KoE": (1404, 1872), "KoF": (1440, 1920), "KoN": (758, 1024),
          "KCS": (1272, 1696), "KPW6": (1272, 1696), "KPW5": (1236, 1648), "KPW34": (1072, 1448),
          "K11": (1072, 1448), "KO": (1264, 1680), "KS": (1860, 2480), "KS3": (1986, 2648), "KSCS": (1986, 2648)}
KOBO_PROFILES = {p for p in SCREEN if p.startswith("Ko")}
KINDLE_PROFILES = set(SCREEN) - KOBO_PROFILES

# The apps to read with, per platform (the guides and the start page show them)
APPS = {
    "ios": {"books": "Apple Books (open a downloaded EPUB), or an OPDS app such as Readest or KyBook 3",
            "comics": "Panels (iPhone and iPad) or Chunky (iPad): add the library's OPDS catalog, or open a downloaded CBZ",
            "audio": "ShelfPlayer, SoundLeaf or AudioBooth (there is no official Audiobookshelf iPhone app yet)"},
    "android": {"books": "KOReader, Moon+ Reader or Readest: add the library's OPDS catalog, or open a downloaded EPUB",
                "comics": "CDisplayEx, Moon+ Reader or KOReader: add the OPDS catalog, or open a downloaded CBZ",
                "audio": "the Audiobookshelf app (Google Play)"},
}


def clean(keys):
    """The known device keys among `keys`, in catalogue order, each once."""
    want = set(keys or [])
    return [m[0] for m in MODELS if m[0] in want]


def chosen(owner):
    """[model dict] this reader reads on (empty: not chosen yet)."""
    import db
    return [BY_KEY[k] for k in db.get_prefs(owner).get("devices") or [] if k in BY_KEY]


def families(owner):
    return {m["family"] for m in chosen(owner)}


def has(owner, family):
    return family in families(owner)


def _area(profile, default):
    w, h = SCREEN.get(profile or default, (0, 0))
    return w * h


def kobo_target(owners):
    """(profile, colour, names) for the ONE Kobo copy of a comic these readers share. Their chosen
    Kobos decide: the sharpest screen, in colour when any of them is a colour Kobo. profile None
    is the host job's configured one (KCC_KOBO_PROFILE), in colour: what a reader who has a Kobo
    but chose no model counts as, and exactly what every copy was before v6.2."""
    default = config.KCC_KOBO_PROFILE
    profiles, names = [], set()
    for o in owners or []:
        kobos = [m for m in chosen(o) if m["family"] == KOBO]
        profiles += [m["profile"] for m in kobos] or [None]
        names |= {m["name"] for m in kobos}
    if all(p is None for p in profiles):                  # also: nobody (a 'Make Kobo copy' alone)
        return None, True, sorted(names)
    colour = any(p is None or p in COLOUR_PROFILES for p in profiles)
    best = max(profiles, key=lambda p: (_area(p, default), p is None or p in COLOUR_PROFILES))
    return best, colour, sorted(names)


def kindle_target(owner):
    """(profile, colour, name) for a comic this reader sends to their Kindle: their first chosen
    Kindle; none chosen (or 'Another Kindle'): profile None, the host job's configured one
    (KCC_KINDLE_PROFILE), in colour, as before v6.2."""
    for m in chosen(owner):
        if m["family"] == KINDLE:
            return m["profile"], (m["profile"] is None or m["profile"] in COLOUR_PROFILES), m["name"]
    return None, True, None


def profile_name(profile):
    """'Kobo Libra Colour' for KoLC (the first model with that profile), else the profile."""
    return next((m[2] for m in MODELS if m[3] == profile), profile or "")
