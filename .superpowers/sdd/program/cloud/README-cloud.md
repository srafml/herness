# Herness program — cloud session pack

This branch (`program-cloud`) carries the program files a cloud session needs; the
main repository ignores `.superpowers/`, so they are published here separately.
A cloud session must NOT merge this branch into any code branch. It reads from a
checkout of it and writes its ledger, report and review files back into the same
folder, then pushes them on a `program-cloud-out/<group>` branch.
