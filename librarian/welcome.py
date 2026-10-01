"""v6.3.1: the welcome e-mail a new family member gets when the admin creates their account
(bookstack.sh Users -> Add a user, or Users -> Send a welcome e-mail again).

It says where to start, which user name to sign in with and what to do first; it NEVER carries
the password (mail is kept, forwarded and read on shared screens: the admin gives it to them in
person or in a message, and they change it on their first visit). Plain text with an HTML part,
worded for someone who has never seen the library; replies go to the admin (ADMIN_EMAIL).

  python -m admin_cli welcome <user> [--preview]"""
import html, re
from email.message import EmailMessage
import config, cwa


class WelcomeError(Exception):
    pass


def _sections(user):
    """[(heading or None, [paragraph])] for this account; the same words in both parts."""
    u = cwa.get_user(user)
    if not u:
        raise WelcomeError(f"no such user '{user}'")
    admin = bool((u.get("role") or 0) & cwa.ROLE_ADMIN)
    home = config.HOME_URL or config.PORTAL_URL
    second = config.AUTHELIA_ENABLED and (admin or config.AUTHELIA_READERS_2FA)
    out = [(None, [f"Hi {user},",
                   "An account is ready for you in the family library: ebooks, audiobooks"
                   + (" and comics" if config.COMICS_ENABLED else "")
                   + ", read on your own Kobo, Kindle, phone or tablet. Here is how to start."])]
    out.append(("Signing in", [
        f"Start here: {home}  (bookmark it: every part of the library is one tap away from there)",
        f"User name: {user}",
        "Password: the one your library admin gave you. For your safety it is never sent by e-mail.",
        "Forgot it one day? Tap \"Forgot password?\" on the sign-in page: an e-mailed link lets you choose a new one, and it "
        "works everywhere at once.",
    ] + (["The first time you sign in you are asked to set up a second step (an authenticator app on your "
          "phone, or a passkey). It takes a minute and keeps your account yours."] if second else [])))
    steps = [
        f"1. Open {home} and sign in.",
        "2. Under \"Your devices\", tick what you read on: a Kobo, a Kindle, a phone or a tablet. The page then "
        "shows what to do for each one, and which apps to use.",
        f"3. Change the password to one of your own: {config.PORTAL_URL}/devices, under Account. Change it only "
        "there, so the library, the audiobook app and the rest all take the new one.",
        "4. Optional: turn on phone notifications on the same page, to hear when a book you asked for arrives.",
    ]
    out.append(("Your first five minutes", steps))
    out += _device_guides(u, home)
    get = [f"Search on the portal ({config.PORTAL_URL}) and press Get it. The library looks for a good copy (the "
           "right book, in your language, in a format your devices read), shows it to you once to confirm, and it "
           "lands in your library, and on a linked Kobo at its next sync."]
    if config.AUDIO_URL:
        get.append(f"Audiobooks: listen in the Audiobookshelf app, or at {config.AUDIO_URL}, with the same user name "
                   "and password.")
    if config.COMICS_ENABLED:
        get.append("Comics and manga: the Comics page on the portal; pick a series and the volumes you want.")
    out.append(("Getting books", get))
    out.append(("Your library is yours", [
        "No other reader can see which books you have or what you read (only the library's admin, who runs it, can). "
        + ("When you ask for a book the library already holds, it is simply added to yours, with nothing "
           "downloaded twice; nothing ever says whose it was. If you would rather your own books were never added "
           "to anyone else's library, tick \"Keep my books private\" under Your settings on the start page."
           if config.FAMILY_SHARING else ""),
    ]))
    if admin:
        out.append(("You are an admin", [
            f"You also see every book in the library, and the admin dashboard ({config.PORTAL_URL}/admin) "
            "shows what needs you. Your own Kobo gets only your own books unless you choose otherwise."]))
    out.append(("Questions", [
        f"Every guide is on the start page ({home}, Guides). Something not working? {home}/help/troubleshooting goes "
        "through signing in, Kobo, Kindle, apps and requests, by what you see. "
        + ("Or simply reply to this e-mail." if config.ADMIN_EMAIL else "Or ask your library admin."),
        "Happy reading!"]))
    return out


def _device_guides(u, home):
    """v6.3.1: setting up a Kobo, a Kindle, a phone or tablet: enough to do it from the mail, with the
    page where it is done and the full guide linked."""
    devices = f"{config.PORTAL_URL}/devices"
    import kindle
    sender = kindle.sender() or "the library's sending address (shown on your Devices page)"
    try:
        kobo_on = cwa.kobo_sync_enabled()
    except Exception:
        kobo_on = True
    kobo = ([
        "Your whole library reaches the Kobo by itself at every sync once it is linked. It takes two minutes and a USB cable:",
        f"1. On your Devices page ({devices}), Kobo: press Generate my Kobo link, then copy the link it shows.",
        "2. Connect the Kobo to a computer with its USB cable. In its drive, open the file .kobo/Kobo/Kobo eReader.conf in a "
        "text editor (on a Mac the .kobo folder is hidden: press Cmd+Shift+. in Finder to show it).",
        "3. Find the line that starts with api_endpoint= (under [OneStoreServices]) and replace everything after the = with "
        "your link. Save, eject the Kobo, and tap Sync.",
        "4. Back on the Devices page, Test my link confirms it works.",
        "While it is linked, the Kobo's own store and OverDrive do not work on it. To keep it tidy (only the books you send, "
        "or finished books leaving it by themselves), see Your settings on the start page.",
    ] if kobo_on else ["Kobo sync is not switched on for this library yet: ask your library admin, then follow the Kobo guide."]) \
        + [f"Full Kobo guide: {home}/help/kobo"]
    kindle_saved = u.get("kindle_mail")
    kindle = [
        "Books reach a Kindle by e-mail (Amazon's Send to Kindle). Three steps, once:",
        "1. Find your Kindle's own e-mail address: on Amazon, Manage Your Content and Devices, Preferences, Personal "
        "Document Settings (https://www.amazon.com/mycd, or your country's Amazon). It ends in @kindle.com.",
        f"2. On that same Amazon page, add {sender} to the Approved Personal Document E-mail List. Amazon drops anything "
        "from an address that is not on it, so this is the step that matters.",
        (f"3. Your Kindle address ({kindle_saved}) is already saved on your Devices page ({devices}): press Send a test "
         "there, and it arrives within a few minutes if step 2 worked." if kindle_saved else
         f"3. On your Devices page ({devices}), Kindle: paste your @kindle.com address, save it, and press Send a test. "
         "It arrives within a few minutes if step 2 worked."),
        "Then press Send to Kindle on any book's page, or turn on automatic sending (Devices, Preferences) to have every new "
        f"book sent. Amazon takes files up to {config.KINDLE_MAX_MB} MB by mail.",
        f"Full Kindle guide: {home}/help/kindle",
    ]
    phone = [
        "Nothing to set up on the library's side: read in an app that connects to the library's catalog. Add this OPDS "
        f"catalog in the app, with your user name and password: {config.BOOKS_URL}/opds/ (keep the / at the end).",
        "iPhone and iPad: Apple Books (open a downloaded EPUB), Panels or Chunky for comics. Android: KOReader, Moon+ Reader "
        "or Readest. Once you tick your phone or tablet under Your devices, the start page lists the apps for it.",
        f"Full guide: {home}/help/phone-tablet",
    ]
    return [("Setting up a Kobo", kobo), ("Setting up a Kindle", kindle), ("Reading on a phone or tablet", phone)]


def compose(user):
    """(subject, plain text, html) for this account."""
    secs = _sections(user)
    subject = "Your family library account is ready"
    text = []
    for head, paras in secs:
        if head:
            text += [head, "-" * len(head)]
        step = lambda x: x[:1].isdigit() and x[1:2] == "."
        for i, p in enumerate(paras):
            text.append(p)
            # a blank line between paragraphs; the sign-in details and a numbered list stay together
            if i < len(paras) - 1 and not (step(p) and step(paras[i + 1]) or p.startswith(("Start here:", "User name:"))):
                text.append("")
        text.append("")
    body = []
    for head, paras in secs:
        if head:
            body.append(f'<h3 style="margin:18px 0 6px;font-size:16px">{html.escape(head)}</h3>')
        for p in paras:
            body.append(f'<p style="margin:0 0 8px">{_link(html.escape(p))}</p>')
    page = ('<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:15px;'
            'line-height:1.5;color:#222;max-width:560px">' + "".join(body) + "</div>")
    return subject, "\n".join(text).rstrip() + "\n", page


_URL = re.compile(r"https://[^\s<>\"')]+[^\s<>\"').,;:]")


def _link(s):
    """Make the addresses in the (already escaped) text clickable."""
    return _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', s)


def send(user):
    """Mail it to the account's own address. Returns the address."""
    import kindle
    if not kindle.configured():
        raise WelcomeError("outgoing mail is not set up (Library -> Mail): nothing was sent")
    u = cwa.get_user(user) or {}
    to = (u.get("email") or "").strip()
    if "@" not in to:
        raise WelcomeError(f"{user} has no e-mail address on file")
    subject, text, page = compose(user)
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if config.ADMIN_EMAIL:
        msg["Reply-To"] = config.ADMIN_EMAIL
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    kindle._deliver(msg)
    return to
