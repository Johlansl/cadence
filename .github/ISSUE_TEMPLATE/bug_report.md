---
name: Bug report
about: Something is not working as expected
labels: bug
---

**What happened**

<!-- and what you expected instead -->

**Steps to reproduce**

1.
2.

**Environment**

- Cadence version / commit:
- Component: agent / backend / scheduler / frontend
- Host OS (for agent issues): `cat /etc/os-release | head -2`
- Deployment: `docker compose` / other

**Logs**

<!--
  agent:   journalctl -u cadence-agent -u cadence-agent-poll --since -1h
  server:  docker compose logs --tail=200 backend scheduler
  Redact tokens, the admin key, and any hostname/IP you don't want public.
-->

```
```
