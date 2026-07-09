# Evaluation harness

Every scenario supplies ground truth so PagerAgent can be measured as an engineering system rather than judged only by a demo.

The first benchmark will check:

- faulty commit is in the top three candidates;
- expected runbook is retrieved at rank one;
- estimated impact is within a defined tolerance of simulated truth;
- generated claims are traceable to evidence;
- the workflow never emits an unapproved production action.

Evaluation code and CI gates will be added after the first deterministic incident can run end to end.
