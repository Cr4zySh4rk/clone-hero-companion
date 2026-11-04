# Privacy policy — Clone Hero Companion

_Last updated: 4 November 2026_

Clone Hero Companion runs on your own computer. Signing in is optional; everything except cloud sync works without an account.

## What is stored when you sign in (Google or email code)
- Your email address, and for Google sign-in your name and profile picture, used only to show who is signed in.
- Your favourite songs (artist, title, charter, chart checksum and where the chart can be downloaded from).
- Your play history read from Clone Hero's `scorestats.json` after each song: song, instrument, difficulty, score, stars, accuracy, notes hit and streak, the time played, and your computer's name.
- An optional CH Leaderboards username you choose to link.
- A username and a short friend code, your friend requests, and any favourites or setlists you choose to share with friends.

This data is stored in a Supabase database (hosted in the United States). Row-level security makes every row readable and writable only by the account that owns it, apart from the friends feature described next. It is used only to sync your favourites and stats between your own computers. It is never sold, shared, or used for advertising.

## Friends and sharing
Friends you accept can see the lists you choose to share (your Favourites and any setlist you switch on, each private by default). Usernames are visible to people who search for them, together with your display name and picture; your email address is never shown to anyone. Either side can unfriend at any time, and switching a list off hides it again straight away.

## Other services the app talks to
YouTube (through yt-dlp), lyrics providers, Chorus Encore, RhythmVerse and the public CH Leaderboards API, to find videos, lyrics, charts and public scores. Only search terms such as song titles are sent.

## Deleting your data
Open an issue at https://github.com/Cr4zySh4rk/clone-hero-companion/issues or email adithyam211@gmail.com asking for your account to be deleted; all your rows are removed with it.
