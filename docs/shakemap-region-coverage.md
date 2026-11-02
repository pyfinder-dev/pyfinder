# ShakeMap regional coverage resources

Each registered file defines a caller-side coverage area in WGS84 geographic
coordinates: longitude first, latitude second, in degrees. Use a nonempty, valid
`POLYGON` or `MULTIPOLYGON`. PyFinder does not repair or reproject these polygons.
Only filenames explicitly registered in `shakemap.regional-configurations` affect
selection. Other files in this directory are not automatically activated.

Region labels and service profile names do not define their geography. An area
may cover parts of several countries, a subregion, or several disjoint areas.
Several registered areas may use the same native ShakeMap profile. Profiles are
chosen from actual WKT coverage, independently of FinDer's regional catalog.

The nine initial files preserve the previous coverage footprints. They were
extracted, without geometry changes, from the existing bundled Natural Earth
1:110m resource `pyfinder/extern/ne_110m_admin_0_countries/`. ISO fields were used only to
identify the seed feature during authoring; the runtime selector never reads
country codes or the country dataset. France was selected through ISO_A2_EH=FR.
A full-precision WKT roundtrip was checked with exact coordinate equality.

| Seed filename | Authoring feature identifier |
| --- | --- |
| albania.wkt | AL |
| belgium.wkt | BE |
| croatia.wkt | HR |
| france.wkt | FR |
| greece.wkt | GR |
| italy.wkt | IT |
| romania.wkt | RO |
| slovenia.wkt | SI |
| switzerland.wkt | CH |

Source artifact SHA-256 identities:

```text
ne_110m_admin_0_countries.shp 08e341606e8391e458c3f08deb312de664b56bfae376064c5aa0aee6681a5f55
ne_110m_admin_0_countries.dbf 1fee677cd4e03b367876e03861eb10197e4022a846bf92060e0313432863785b
ne_110m_admin_0_countries.shx 8b0be2ad97dd484aee5c2ebc98697d5372e832b8ae58a35a661aeef6b985668d
ne_110m_admin_0_countries.prj 3259f0e55290a82b1350646f604e8a7ee1e2136c0320a40fad838ab40819fff8
```

These coarse seed footprints preserve existing geographic coverage; they do not
qualify a native regional scientific model. Edit or add separately reviewed WKT
resources and update the catalog to supply the intended arbitrary regional
coverage. Rebuild/redeploy the caller to install those changes. No FinDer WKT
resource or service-owned native profile is copied or modified by this catalog.
