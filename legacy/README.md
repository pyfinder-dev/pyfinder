# Retained ShakeMap implementation

`shakemap.py` is the unchanged former `pyfinder/utils/shakemap.py`, retained
before the separate-service adapter was introduced. It contains the old input
exporter, local profile replacement, local `shake` execution, and product ZIP
collection. Keep it for reference until the project owner chooses to remove it.

It is not an active fallback and should not be imported by the application.
Its original imports, paths, and known defects are intentionally preserved;
this copy is historical source, not a supported runnable utility. The existing
package and Docker-context rules exclude this directory from distribution.

Related configuration-fetching and region-lookup modules remain in their
original locations. They have not been deleted or silently replaced.

`manager-downstream-reference.md` preserves the removed commented call sequences
from the manager and scheduler, including local configuration setup and email
construction. It complements the unchanged module without duplicating it.
