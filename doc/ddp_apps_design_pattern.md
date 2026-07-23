App conventions:

* An app must realise at least of the 3 execution modes:
    1. Training
    2. Offline computation
    3. Online Computation (Loading and serving)

* Apps operate "independently" on each charter

* An app's Offline phase should produce one file per charter or image, the app and its maintainer is the sole "owner" of that file

* An app that reads anothers apps output is deemed to depend on the app owning it

* App's offline computation mode should work as a "map" operation on charters (or images) while any reduction operation should be happing when loading online computation

* If loading online computation is to slow and cand be improveed by better engineering an intermediary cache can be employed

* Mom Vocabularies should be be implemented as apps, probably the same goes for other auxiliary tables other than charters, images, fonds (including collections), and archives.

* Each app defines a glob filepattern it owns.
