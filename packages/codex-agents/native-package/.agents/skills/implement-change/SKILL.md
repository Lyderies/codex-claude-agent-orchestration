---
name: implement-change
description: Prepare one bounded code patch governed by accepted project decisions for the primary agent to apply and validate.
---

# Implement change

Read the governing project instructions and accepted decisions. Confirm the assigned
files or responsibility, then prepare the smallest complete unified patch. Remain
read-only: the primary agent applies the patch after checking it against current files.
Do not broaden architecture or authority. Use fake boundary designs for external
providers, secrets, money, or destructive effects. Inspect existing tests and state the
focused commands the primary must run after applying the patch. Report proposed files,
the exact patch, validation still required, and remaining risks; leave application and
independent verification to other agents.
