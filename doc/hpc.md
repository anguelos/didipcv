#### GSC1
* [entrypoint1](l1.gsc1.uni-graz.at)
* [entrypoint2](l2.gsc1.uni-graz.at)

```bash
CLUSTER=l1.gsc1.uni-graz.at
UNIGRAZUSER=nicolaou

```



#### Sauron1
Sauron is located in uni-graz

* Located in Uni graz
* Contact david.bodruzic@uni-graz.at
* Documented GPU node: 143.50.10.65 (10.50.10.65)
* Undocumented GPU node: (ubuntu 20.04) (10.143.10.68)

* Documentation:
	** [harware list](https://hpc-wiki.uni-graz.at/_layouts/15/WopiFrame.aspx?sourcedoc={88A39351-B446-4D1B-9B21-84F8F5E8CAFD}&file=Sauron_Summary.pdf&action=default)
	** [Quick intro](https://hpc-wiki.uni-graz.at/_layouts/15/WopiFrame.aspx?sourcedoc={9EBE1254-899E-4C25-B7CB-4A25A1550935}&file=GPU_Server.pdf&action=default)



###### Request inreactive shell with GPU
```bash
UNIGRAZ_LOGIN=nicolaou
ssh "${UNIGRAZ_LOGIN}@sauron1.uni-graz.at" qlogin -l ngpus=1
```




###### Setup spack
[Spack](https://spack.readthedocs.io/en/latest/) is a userland package management aimed at clusters.
```bash
UNIGRAZ_LOGIN=nicolaou
CLUSTER=sauron1.uni-graz.at
ssh  "${UNIGRAZ_LOGIN}@${CLUSTER}"
git clone -c feature.manyFiles=true https://github.com/spack/spack.git
./bin/spack install byobu
```

##### Use spack
```bash
UNIGRAZ_LOGIN=nicolaou
CLUSTER=sauron1.uni-graz.at
ssh "${UNIGRAZ_LOGIN}@${CLUSTER}"
eval `./spack/bin/spack load --sh   byobu`
byobu
```



