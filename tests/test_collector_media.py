"""Collector media resolution: path containment and match evidence."""


def test_resolver_refuses_paths_outside_the_allowed_roots(tmp_path):
    # Media paths come out of the QQ database, so a row naming any file on the
    # disk would otherwise be read and uploaded as that message's media.
    from collector.media.resolver import MediaResolver

    qq_root = tmp_path / "qq"
    qq_root.mkdir()
    inside = qq_root / "photo.jpg"
    inside.write_bytes(b"inside the data root")
    outside = tmp_path / "secret.key"
    outside.write_bytes(b"definitely not media")

    unrestricted = MediaResolver()
    assert unrestricted.resolve({"local_path": str(outside)}).full_file == outside.resolve()

    restricted = MediaResolver(allowed_roots=(qq_root,))
    assert restricted.resolve({"local_path": str(inside)}).full_file == inside.resolve()

    refused = restricted.resolve({"local_path": str(outside)})
    assert refused.full_file is None
    # An explicit path that is refused is reported as missing, not silently dropped.
    assert refused.explicit_local_path_missing is True
    assert refused.failure_code == "MEDIA_SOURCE_MISSING"


def test_resolver_rejects_traversal_out_of_an_allowed_root(tmp_path):
    from collector.media.resolver import MediaResolver

    qq_root = tmp_path / "qq"
    (qq_root / "nested").mkdir(parents=True)
    outside = tmp_path / "secret.key"
    outside.write_bytes(b"definitely not media")

    restricted = MediaResolver(allowed_roots=(qq_root,))

    assert restricted.resolve({"local_path": str(qq_root / "nested" / ".." / ".." / "secret.key")}).full_file is None


def test_media_index_needs_a_name_or_checksum_not_just_a_size(tmp_path):
    # A 5% size window over every indexed file matches whatever happens to be
    # close, and the collector archives that as the message's media.
    from collector.media.indexer import MediaFileIndex

    root = tmp_path / "media"
    root.mkdir()
    (root / "real.jpg").write_bytes(b"x" * 1000)
    index = MediaFileIndex([root])
    index.build()

    assert index.find(declared_size=1000) is None
    assert index.find(file_name="real.jpg", declared_size=1000) == (root / "real.jpg").resolve()
