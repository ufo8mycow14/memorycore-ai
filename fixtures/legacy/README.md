# Legacy migration fixture

I retain the original 0.7.1-alpha Python implementation here as an unchanged test fixture. The migration tests read its original schema, and the synthetic evaluator uses it as a historical comparison. This makes those checks independent of my workstation’s recovery folders.

I do not recommend this legacy implementation for use as the current runtime. It lacks later scope, retention and security corrections. All comparisons use synthetic data in disposable storage.
