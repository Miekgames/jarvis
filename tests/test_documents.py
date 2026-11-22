"""Asking questions of the owner's own documents."""
from __future__ import annotations

import os
import time

import pytest

from backend.documents.chunker import MAX_CHARS, MAX_WORDS, OVERLAP_WORDS, chunk
from backend.documents.index import DocumentIndex, _query, read_text


# ===========================================================================
# cutting documents into passages
# ===========================================================================
def test_passages_follow_sections():
    text = "# Trip\n\nWe leave on Friday.\n\n## Budget\n\nThe budget is 400 pounds.\n\nTrains only."
    passages = chunk(text)
    assert [p.heading for p in passages] == ["Trip", "Budget"]
    assert "400 pounds" in passages[1].text and "Trains only" in passages[1].text
    assert "Friday" not in passages[1].text, "no overlap across a heading"


def test_long_text_is_capped_with_overlap():
    sentences = " ".join(f"Sentence number {n} talks about topic {n}." for n in range(400))
    passages = chunk(sentences)
    assert len(passages) > 3
    for passage in passages:
        assert len(passage.text.split()) <= MAX_WORDS + OVERLAP_WORDS
        assert len(passage.text) <= MAX_CHARS + 400
    # The end of one passage is repeated at the start of the next.
    tail = passages[0].text.split()[-5:]
    assert " ".join(tail) in passages[1].text


def test_text_without_spaces_is_still_cut():
    passages = chunk("x" * 10000)
    assert all(len(p.text) <= MAX_CHARS for p in passages)


def test_queries_cannot_inject_search_syntax():
    q = _query('budget" OR path:* NEAR( "trip')
    assert q.count('"') % 2 == 0
    assert "NEAR(" not in q.replace('"near"', "")


# ===========================================================================
# reading formats
# ===========================================================================
def test_word_documents_are_read_with_their_headings(tmp_path):
    import docx

    document = docx.Document()
    document.add_heading("Warranty", level=1)
    document.add_paragraph("The boiler is covered for five years from installation.")
    path = tmp_path / "boiler.docx"
    document.save(str(path))

    text = read_text(path)
    assert "## Warranty" in text and "five years" in text


def test_pdfs_are_read_page_by_page(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (The lease ends in March 2027.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "lease.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)

    text = read_text(path)
    assert "## Page 1" in text and "March 2027" in text


# ===========================================================================
# the index
# ===========================================================================
@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "trip.md").write_text(
        "# Lisbon trip\n\nFlights booked for 12 May. The hotel is Casa Azul in Alfama.",
        encoding="utf-8")
    (root / "boiler.txt").write_text(
        "Boiler service is due every October. Engineer: Dave, 07700 900123.",
        encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config.txt").write_text("hidden boiler stuff", encoding="utf-8")
    (root / "photo.jpg").write_bytes(b"\xff\xd8not text")
    return root


@pytest.mark.asyncio
async def test_a_folder_is_indexed_and_searchable(db, folder):
    index = DocumentIndex(db)
    await index.add_folder(str(folder))
    await index.index_folder(str(folder.resolve()))

    hits = await index.search("when is the boiler service due?")
    assert hits[0]["file"] == "boiler.txt" and "October" in hits[0]["passage"]

    hits = await index.search("which hotel in Lisbon")
    assert hits[0]["file"] == "trip.md" and hits[0]["section"] == "Lisbon trip"

    stats = await index.stats()
    assert stats["files"] == 2, "hidden folders and photos are skipped"
    row = (await index.folders())[0]
    assert row["status"] == "ready" and row["files"] == 2


@pytest.mark.asyncio
async def test_reindexing_reads_only_what_changed_and_drops_what_went(db, folder):
    index = DocumentIndex(db)
    await index.add_folder(str(folder))
    root = str(folder.resolve())
    await index.index_folder(root)

    (folder / "boiler.txt").unlink()
    trip = folder / "notes" / "trip.md"
    trip.write_text("# Lisbon trip\n\nFlights moved to 14 May.", encoding="utf-8")
    later = time.time() + 5
    os.utime(trip, (later, later))
    await index.index_folder(root)

    assert await index.search("boiler service") == []
    hits = await index.search("Lisbon flights")
    assert "14 May" in hits[0]["passage"] and "12 May" not in hits[0]["passage"]


@pytest.mark.asyncio
async def test_removing_a_folder_removes_its_passages(db, folder):
    index = DocumentIndex(db)
    await index.add_folder(str(folder))
    root = str(folder.resolve())
    await index.index_folder(root)
    assert await index.remove_folder(root)
    assert await index.search("boiler") == []
    assert (await index.stats())["passages"] == 0


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_read_is_recorded_not_fatal(db, folder):
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    index = DocumentIndex(db)
    await index.add_folder(str(folder))
    await index.index_folder(str(folder.resolve()))
    assert (await index.stats())["unreadable"] == 1
    assert (await index.folders())[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_the_tool_answers_with_files_named(db, folder):
    from backend.ai.tools import build_registry

    index = DocumentIndex(db)
    await index.add_folder(str(folder))
    await index.index_folder(str(folder.resolve()))
    registry = build_registry({"documents": index})

    result = await registry.get("search_documents").handler(
        registry.context, question="boiler engineer phone number")
    assert result.ok and result.data["passages"][0]["file"] == "boiler.txt"
    assert "name the file" in result.message


@pytest.mark.asyncio
async def test_with_nothing_indexed_the_tool_says_how_to_start(db):
    from backend.ai.tools import build_registry

    registry = build_registry({"documents": DocumentIndex(db)})
    result = await registry.get("search_documents").handler(registry.context, question="x")
    assert not result.ok and "Documents" in result.message
