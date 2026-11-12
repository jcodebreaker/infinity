---
owner: local
me: alex@example.com
---

<!--
A dev file mailbox (ADR 0039, "File mailbox for development"). Never a production provider.

Use: copy this file where the engine can read it (for example into the engine's /data volume) and
set OMNIGENT_DEV_MAILBOX to that path inside the engine. Appending a "## " section is a new mail
arriving; the next poll (OMNIGENT_MAIL_POLL_SECONDS, e.g. 30, for a quick loop) picks it up.
A message dated more than 14 days before it is first seen is only marked seen, so move the dates
close to today before a run.

What each message should do:
  1  Alex's own message: starts the thread, never a signal by itself.
  2  (a) Dana replies in Alex's thread: signal (mail:morocco-landing-pages).
  3  (b) Newsletter with List-Id: dropped as noise.
  4  (c) No-reply notification: dropped as noise.
  5  (d) Sam asks for the Polaris deck numbers: signal on "Polaris overview deck" when that topic
         watches its terms (two of them, or Sam on its watch list); dropped as unrelated otherwise.
  6  (e) Dana again in thread (a): folds into the same signal (one per thread, newest wins).
  7  (f) A personal mail from someone on no watch list: dropped as unrelated.
-->

## 1. Alex starts the Morocco thread
From: Alex Kim <alex@example.com>
To: Dana Reyes <dana@example.org>
Date: 2026-10-23T16:40:00+02:00
Subject: Morocco landing pages: three versions
Thread: morocco-landing-pages

Hi Dana,

The three Morocco landing pages are ready for review: morocco-a.html (short form),
morocco-b.html (long form with the FAQ) and morocco-c.html (video first). Tell me which one
you want to keep and I will retire the other two.

Alex

## 2. (a) Dana replies: which page to keep, and the Egypt link
From: Dana Reyes <dana@example.org>
To: Alex Kim <alex@example.com>
Date: 2026-10-26T09:12:00+01:00
Subject: Re: Morocco landing pages: three versions
Thread: morocco-landing-pages

Hi Alex,

Thanks for these. Before I sign off: which of the three Morocco landing pages do you
recommend we keep? I lean towards the long form but I'd like your view on conversion.

Also, when do I get the link to the Egypt landing page? Our partners there are asking.

Dana

> The three Morocco landing pages are ready for review.

## 3. (b) A newsletter
From: Growth Weekly <digest@news.example.net>
To: alex@example.com
Date: 2026-10-26T07:00:00+00:00
Subject: Ten landing page tricks for Morocco and Egypt campaigns
Thread: growth-weekly-2026-43
List-Id: <growth-weekly.news.example.net>
Precedence: bulk

This week: landing pages that convert, hero videos, and what changed in paid search.
Unsubscribe at any time.

## 4. (c) A no-reply notification
From: Shared Drive <no-reply@drive.example.com>
To: alex@example.com
Date: 2026-10-26T08:05:00+00:00
Subject: "Polaris overview deck" was shared with you
Thread: drive-share-polaris

Sam Ortiz shared "Polaris overview deck" with you. Open it in Shared Drive.

## 5. (d) Sam asks for the Polaris numbers
From: Sam Ortiz <sam@example.org>
To: Alex Kim <alex@example.com>
Cc: Priya Shah <priya@example.org>
Date: 2026-10-26T10:02:00+01:00
Subject: Numbers for the Polaris overview deck
Thread: polaris-numbers

Hi Alex,

Could you send me the latest numbers behind the Polaris overview deck? I need the pipeline
total and the three regional figures for slide 4 before Wednesday's review.

Thanks,
Sam

## 6. (e) Dana follows up a few minutes later
From: Dana Reyes <dana@example.org>
To: Alex Kim <alex@example.com>
Date: 2026-10-26T09:19:00+01:00
Subject: Re: Morocco landing pages: three versions
Thread: morocco-landing-pages

One more thing: if we keep the long form, can the FAQ mention the Egypt landing page too?

Dana

## 7. (f) A personal mail
From: Robin Hale <robin@mail.example.net>
To: alex@example.com
Date: 2026-10-26T12:30:00+01:00
Subject: Book club on Thursday?
Thread: book-club-thursday

Hey! Are you still coming on Thursday? I can bring the second book if you have not
finished the first one yet.
