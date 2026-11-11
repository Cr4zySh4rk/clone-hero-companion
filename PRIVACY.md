# Privacy policy — Clone Hero Companion

_Last updated: 11 November 2026_

Clone Hero Companion runs on your own computer. Signing in is optional; everything except cloud sync works without an account.

## What is stored when you sign in (Google or email code)
- Your email address, and for Google sign-in your name and profile picture, used only to show who is signed in.
- Your favourite songs (artist, title, charter, chart checksum and where the chart can be downloaded from).
- Your play history read from Clone Hero's `scorestats.json` after each song: song, instrument, difficulty, score, stars, accuracy, notes hit and streak, the time played, and your computer's name.
- An optional CH Leaderboards account link. You sign in on CH Leaderboards' own page; the server verifies it with CH, stores your CH account id, username and public profile, and ends the CH session. Your CH password and sign-in token are never stored.
- A career snapshot on your profile row (your totals, star and full combo counts; the public stats and ranks of your verified CH Leaderboards account are stored separately by the server), uploaded by the app so the Career page can show them. Only your own account can read it.
- A username and a short friend code, your friend requests, and, if you turn on sharing, your favourites and setlists as seen by your friends.

This data is stored in a Supabase database (hosted in the United States). Row-level security makes every row readable and writable only by the account that owns it, apart from the friends feature described next. It is used only to sync your favourites and stats between your own computers. It is never sold, shared, or used for advertising.

## Friends and sharing
Sharing is off by default. If you turn on sharing, all your friends can see your favourites and setlists (every setlist, including ones you create later); turn it off and none of them can. Usernames are visible to people who search for them, together with your display name and picture; your email address is never shown to anyone. Either side can unfriend at any time, and switching sharing off hides everything again straight away.

## Other services the app talks to
YouTube (through yt-dlp), lyrics providers, Chorus Encore, RhythmVerse and the public CH Leaderboards API, to find videos, lyrics, charts and public scores. Only search terms such as song titles are sent.

## Anonymous usage statistics
The app and this website count how they are used, so development can focus on what matters. It is anonymous.

**App.** Anonymous usage statistics are always on in the app (from 2.8.1 there is no setting to turn them off). You must accept the [Terms of Use](TERMS.md) and this Privacy Policy on first launch (and again after material changes) before the app can be used; nothing is sent before you accept. What is sent:
- A random install ID (a random UUID kept in the app's config folder, not derived from your hardware or account) and a random ID for each run of the app.
- The app version, operating system family (windows, macos, linux), CPU architecture, and the app language.
- At start: whether you are signed in (yes/no), a library size bucket (0, 1-50, 51-200, 201-1000, 1000+) and the kind of video acceleration (cpu, nvidia, apple, d3d11, vaapi).
- Which screen is open (a fixed list such as library, charts, settings), about every 5 minutes while the app is open, and when you switch screens; and the length of the run in seconds when you quit.
- Counts of actions, with at most a fixed choice attached: chart download (Encore or other), video added or failed, lyrics added or failed, video conversion (number bucket), Spotify import, setlist created, favourite on/off, stats shared (to which service), friend request sent, friend profile viewed, CH account link (ok or failed), app update (ok or failed), job cancelled, game launched.

**Website.** One page view per page load: the page path without any query, the domain you came from (if external), a screen-width bucket, and a random ID kept in your tab's sessionStorage (gone when the tab closes). Also clicks on download buttons (which system) and the Career sign-in method (Google or email). No cookies and no localStorage. Nothing is sent if your browser sends Do Not Track or Global Privacy Control.

**Never collected:** song titles, artists, file paths, search text, usernames, names, email addresses or IP addresses (the server does not record them). Events can only carry the fixed properties above; the server rejects anything else.

**Retention.** Raw events are deleted after 180 days. Only the site owner can read the aggregated figures, through an admin-only dashboard; individual events are not readable by anyone through the app or website.

## Deleting your data
Open an issue at https://github.com/Cr4zySh4rk/clone-hero-companion/issues or email adithyam211@gmail.com asking for your account to be deleted; all your rows are removed with it.
