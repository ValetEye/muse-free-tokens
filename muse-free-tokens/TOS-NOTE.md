# TOS note — please read before using

**Date of this assessment:** 2026-09-22
**Terms reviewed:** the Muse Supplemental Terms of Service
(https://muse.ai/terms, "last updated September 8, 2026"), which incorporate
the Meta Terms of Service and Meta AI Terms of Service.

This document is the author's good-faith reading of those terms **as they
existed on the date above**. It is not legal advice.

## What this software does

`muse-free-tokens` is a local, single-user bridge. It exposes an
OpenAI-compatible HTTP endpoint on the user's own machine; behind it, the
user's **own** Muse bot — operating under the user's **own** Muse account
and subscription — answers the user's **own** prompts. No third-party
service is involved, no credentials are shared, nothing is resold, and by
default nothing leaves localhost.

## Why the author believes it complies (as of 2026-09-22)

Section references are to the Supplemental Terms' §4 "Restrictions":

- **§4.6 (third-party terms, circumvention).** The bridge does not violate,
  bypass, or circumvent any third party's terms, paywalls, CAPTCHAs, access
  controls, or technical protection measures, and it uses no third-party
  credentials. There is no third party in the loop at all.
- **§4.12 (misrepresenting AI output).** Replies are served as model output
  under the model id `muse-free-tokens`. They are never presented as
  human-written, and the software takes no step to conceal that a bot
  produced them.
- **§4.13 (impersonation / deception).** Nothing impersonates any person,
  company, or entity; there is no phishing, fraud, or misinformation
  functionality.
- **§4.16 (concealed cross-system automation).** The automation (queue →
  agent → reply) is set up by the single user for their own use. It is not
  concealed from any affected party, because the only affected party is the
  user who configured it.
- **§4.17 (extracting Muse).** The software does not extract model weights,
  distill behavior, or bypass Meta's access controls. It routes the user's
  own prompts to the user's own bot through normal use of the product, for
  personal use only. It provides no mechanism for sharing, reselling, or
  redistributing access to Muse.
- **§4.18 / §4.20 / §4.22 (unauthorized access, reverse engineering,
  safety bypass).** The bridge accesses only the user's own account, performs
  no reverse engineering of Muse, and contains nothing that encourages or
  enables bypassing Muse's safety or privacy systems.
- **§6 (connected services).** The only "connected" party is the user's own
  Muse bot. No third-party service's terms are implicated by the software.
- **§7 (user responsibility).** The user configures and supervises their own
  agent; artifacts the agent produces are the user's responsibility, as the
  terms already provide.

In short: this is a personal-use convenience bridge — comparable in kind to
using Muse's own chat interface, over a different transport — not a product
built *out of* Muse for others.

## Your obligations (read carefully)

1. **Verify for yourself, at your own risk and responsibility.** The above
   is one person's reading, not a guarantee. Meta may interpret its terms
   differently, and only you are responsible for your compliance (§7).
2. **Watch for terms changes.** Meta may update these terms at any time
   (§20). If a change makes this software a violation — or if you are unsure
   whether it does — **you must stop using it** until you have verified
   otherwise.
3. **Do not use this to share or resell access.** The assessment above
   assumes strictly personal use: your bot, your account, your prompts. If
   you expose the endpoint to others, charge for it, or bundle it into a
   product, this assessment does not cover you.
4. **Enforcement is real.** Under §17, Meta may suspend or terminate access
   for violations, without notice.

If any of this makes you uncomfortable, do not use the software.
