# Brand assets

Home Assistant does not load integration logos from the integration itself. The
frontend fetches them from `brands.home-assistant.io`, which is served from the
[home-assistant/brands](https://github.com/home-assistant/brands) repository.
Until these images are merged there, the integration shows a generic placeholder
in the UI — there is no way to ship a logo inside a custom component.

## Submitting

Fork `home-assistant/brands`, then copy this directory into place and open a PR:

```bash
cp -r brands/custom_integrations/yonkers_waterwise \
      /path/to/brands/custom_integrations/
```

The PR needs to state that this is a custom integration and link to
https://github.com/Safrone/yonkers_water_wise.

## What's here

| File | Size | Contents |
| --- | --- | --- |
| `icon.png` | 256×256 | Owl mark only, centred on a transparent square |
| `icon@2x.png` | 512×512 | Same, double resolution |
| `logo.png` | 980×256 | Full lockup: owl plus "YONKERS waterwise" |
| `logo@2x.png` | 1960×512 | Same, double resolution |

All are RGBA PNGs with transparent backgrounds, trimmed to the subject.

## Provenance

Derived from the City of Yonkers WaterWise logo, published by the city at
`https://www.yonkersny.gov/ImageRepository/Document?documentId=15647`
(2100×600 RGBA PNG).

The icon was produced by isolating the owl through connected-component analysis
of the source alpha channel, rather than by cropping — the "Y" of YONKERS
overlaps the owl's brow horizontally, so no straight cut separates them cleanly.
Components kept: the brow, the face and beak, and the two eyes.

The logo is the full artwork, trimmed to its alpha bounding box and scaled so the
shortest side is 256 px (512 px for `@2x`), as the brands guidelines require.

Note that this is a municipal logo belonging to the City of Yonkers. It is used
here to identify the utility the integration talks to; the integration is not
affiliated with or endorsed by the city.
