# The Sitekeeper handbook

`index.html` is the whole site. One file, no build step, no server-side
anything: drop it on any static host, behind any path, and it works. Fonts come
from Google Fonts; everything else - styles, script, icons - is inline, so it
also opens correctly straight off a disk.

It holds two things:

- **the handbook** - everything Sitekeeper does, indexed down the left;
- **the hosting-provider tutorial** - the contract a control panel implements to
  hand connections to its customers in one click, which is the same material as
  [`../HOSTING_PROVIDERS.md`](../HOSTING_PROVIDERS.md) written for reading
  rather than for a repository.

## Hosting it

```
cp wiki/index.html /var/www/wiki/index.html
```

That is the deploy. Any subdirectory works - every link on the page is a
fragment, so nothing depends on where it is mounted.

## Keeping it current

The page states a version in three places: the title block, the badge in the top
bar, and the footer. When Sitekeeper's version changes, so do those. The
provider contract is the part most worth keeping honest - if
`mysql_runner/storage/provisioning.py` and this page ever disagree, the code is
right and the page is stale.
