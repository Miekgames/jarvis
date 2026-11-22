"""Working unattended.

JARVIS can be given a job and left to it for hours. That is a genuinely
different situation from a conversation, and the difference is not the
length -- it is that **nobody is there to say no**.

Every safeguard elsewhere in this codebase ends in "and then it asks". Risky
tool calls raise a confirmation; the interface shows it; a person decides.
Remove the person and that design does not degrade gracefully, it inverts:
the thing that used to be a checkpoint becomes either a blocker that stops
all work, or -- far worse -- something quietly auto-approved because the run
had to continue.

So unattended work is built around a third answer. An action that would have
asked is **held**: not performed, not approved, written down with its reason
and left for the owner. The run carries on with everything it can do without
it. What comes back is finished work plus an honest list of what it would
not do alone.

The pieces:

  `guard`    what may happen unattended, and what is held
  `session`  the run itself -- budgets, phases, journal, stopping
"""
