#!/usr/bin/env python3
"""Compare the pinned upstream reference(s) with the newest available one.

Environment:
  UPSTREAM_IMAGES  space separated image references without tag/digest; the
                   first one is queried, all are pinned to the same tag. A
                   reference "github.com/<owner>/<repo>" means a source-built
                   upstream: its git tags are listed instead of image tags
  TAG_REGEX        anchored regex a version tag must match; the named group
                   `ver` holds the part whose numbers are compared.
                   Empty = digest mode: the image is pinned by digest and
                   compared with the current digest of its `latest` tag
                   (for upstreams that do not tag releases)
  PIN_FILES        files holding the pinned references (default build-images.sh)
Writes current / newest / newer to $GITHUB_OUTPUT and the sed commands that
perform the bump to .upstream_bump.sh.
"""
import json
import os
import re
import subprocess
import sys

images = os.environ["UPSTREAM_IMAGES"].split()
pattern = os.environ.get("TAG_REGEX", "")
pin_files = os.environ.get("PIN_FILES", "build-images.sh").split()
digest_mode = pattern == ""
regex = re.compile(pattern) if pattern else None

def skopeo(*args):
    return json.loads(subprocess.run(["skopeo", *args], capture_output=True, text=True, check=True).stdout)

def pinned(image):
    for f in pin_files:
        for line in open(f):
            if line.lstrip().startswith("#"):
                continue  # comments may mention the image with a placeholder tag
            m = re.search(rf'{re.escape(image)}([:@][A-Za-z0-9_.:-]+)', line)
            if m:
                return m.group(1)
    print("pinned reference not found for", image, "in", pin_files, file=sys.stderr); sys.exit(1)

bumps = []  # (image, current_suffix, new_suffix)
if digest_mode:
    for img in images:
        cur = pinned(img)
        info = skopeo("inspect", f"docker://{img}:latest")
        new = "@" + info["Digest"]
        bumps.append((img, cur, new, info.get("Created", "")[:10]))
    current = bumps[0][1].lstrip("@")[7:19]
    newest = bumps[0][2].lstrip("@")[7:19] + f" (latest build {bumps[0][3]})"
    newer = any(c != n for _, c, n, _ in bumps)
else:
    def numeric(tag):
        return [int(x) for x in re.findall(r"\d+", regex.match(tag).group("ver"))]
    if images[0].startswith("github.com/"):
        # source-built upstream: the "image" is a GitHub repository, versions are its git tags
        import urllib.request
        repo = images[0][len("github.com/"):]
        all_tags = []
        for page in range(1, 6):
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/tags?per_page=100&page={page}",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": "ns8-upstream-check"})
            if os.environ.get("GITHUB_TOKEN"):
                req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
            batch = json.load(urllib.request.urlopen(req, timeout=30))
            all_tags += [t["name"] for t in batch]
            if len(batch) < 100:
                break
        tags = [t for t in all_tags if regex.match(t)]
    else:
        tags = [t for t in skopeo("list-tags", f"docker://{images[0]}")["Tags"] if regex.match(t)]
    if not tags:
        print("no version tags found for", images[0], file=sys.stderr); sys.exit(1)
    newest_tag = max(tags, key=numeric)
    current_tag = pinned(images[0]).lstrip(":")
    newer = regex.match(current_tag) is not None and numeric(newest_tag) > numeric(current_tag)
    current, newest = current_tag, newest_tag
    bumps = [(img, ":" + current_tag, ":" + newest_tag, "") for img in images]

with open(".upstream_bump.sh", "w") as fp:
    fp.write("set -e\n")
    for img, cur, new, _ in bumps:
        for f in pin_files:
            fp.write(f"sed -i 's|{img}{cur}|{img}{new}|g' '{f}'\n")
print(f"current={current} newest={newest} newer={newer}")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as fp:
    fp.write(f"current={current}\nnewest={newest}\nnewer={'true' if newer else 'false'}\n")
