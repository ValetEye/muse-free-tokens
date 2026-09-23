# TOS note — please read before using

**Date of this assessment:** 2026-09-22
**Terms reviewed:** the Muse Supplemental Terms of Service
(https://muse.ai/terms, page text "Last updated: September 8, 2026"),
which incorporate the Meta Terms of Service and Meta AI Terms of
Service.

This document is the author's good-faith reading of those terms **as they
existed on the date above**. It is not legal advice.

The live page names headings (“Restrictions”, “Using Connected
Services”, “User Responsibility”, “Enforcement, Termination and
Suspension” / Section 17, “Changes to These Terms”). It does **not**
print decimal Restriction cites such as `§4.6`. Older drafts of this
note counted Restriction bullets and labeled them `§4.x`. Those numbers
were not Meta's labels. This note no longer uses them.

## What this software does

`muse-free-tokens` is a local, single-user mailbox. It exposes an
OpenAI-compatible HTTP endpoint on the user's own machine. The user's
**own** Muse agent — under the user's **own** Muse account — polls that
mailbox and answers the user's **own** prompts.

Default bind is loopback. The supported Muse path is **not** loopback:
Muse runs in Meta's cloud VM, so the user pastes a standing prompt
(including the bearer token) into Muse and publishes the mailbox through
a tunnel or Tailscale URL that Muse can open. Meta therefore sees the
prompts and the token the user chose to give their agent. A tunnel
operator may see connection metadata. Nothing is resold by this
software. Sharing the token or the public URL lets someone else spend
that account.

## Why the author believes personal use can comply (as of 2026-09-22)

Read the live Restrictions list yourself. The author's reading of the
relevant bullets, in the author's own words:

- The bridge does not scrape, bypass paywalls, or use someone else's
  credentials. It uses the user's own Muse account.
- Replies are served as model output under the model id
  `muse-free-tokens`. They are not presented as human-written.
- The software has no phishing, impersonation, or fraud feature.
- The queue → agent → reply loop is set up by the same user who owns
  the account. It is not hidden from that user.
- The Restriction that forbids distillation is worded “Distill, or
  attempt to distill, Muse”, not “extract weights.” This bridge does not
  extract weights or distill Muse. It routes the user's prompts to the
  user's own bot. Meta may still treat a mailbox plus a tunnel as
  something other than “normal use.” That risk is yours.
- The bridge does not reverse engineer Muse or turn off Muse's safety
  systems.

“Using Connected Services” and “User Responsibility” still apply: you
configure the agent; you are responsible for what it does.

In short: this is a personal-use convenience bridge, not a product
built out of Muse for other people. It is **not** the official Meta
Model API (`https://api.meta.ai`). Using this bridge as a stand-in for
that paid API is a risk you take yourself.

## Your obligations (read carefully)

1. **Verify for yourself.** The above is one person's reading, not a
   guarantee. Meta may interpret its terms differently. Only you are
   responsible for your compliance.
2. **Watch for terms changes.** Meta may update the terms at any time.
   If a change makes this software a violation — or if you are unsure —
   **stop using it** until you have verified otherwise.
3. **Do not share or resell access.** This reading assumes strictly
   personal use: your bot, your account, your prompts. If you expose
   the endpoint to others, charge for it, or bundle it into a product,
   this assessment does not cover you.
4. **Enforcement is real.** Section 17 of the Supplemental Terms allows
   Meta to suspend or terminate access without prior notice.

If any of this makes you uncomfortable, do not use the software.
