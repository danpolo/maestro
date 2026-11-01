# Operation receipt store

`apply(root, operation_id)` represents a non-idempotent external action. The local store must:

- reject an empty operation ID;
- keep every state path inside the resolved `root` directory;
- refuse corrupt state rather than assuming no prior effect;
- serialize concurrent writers;
- make a retry with the same operation ID return the original effect count;
- preserve an explicit uncertain outcome when the effect may have happened but its receipt was not persisted.

The review target is `candidate.py`. Report correctness or recovery defects with concrete code evidence. Do not modify files and do not use external research.
