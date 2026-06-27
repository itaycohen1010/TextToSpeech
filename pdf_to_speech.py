"""
PDF to Speech Pipeline — Hebrew-first
Usage: python pdf_to_speech.py <path/to/file.pdf>

Outputs (same directory as input):
  <stem>_segments.txt  — extracted text split into numbered segments
  <stem>_speech.mp3    — final concatenated audio

Requires:
  pip install -r requirements.txt
  ffmpeg installed and on PATH
"""

import io
import os
import re
import sys
from pathlib import Path

VOICE_LANGUAGE = "he-IL"
VOICE_NAME = "he-IL-Wavenet-B"   # male; alternatives: Wavenet-A/C/D
MAX_BYTES = 4500  # Google Cloud TTS limit is 5000 bytes; stay safely below


def extract_text(pdf_path: Path) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install PyMuPDF")

    doc = fitz.open(str(pdf_path))
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    page_count = len(doc)
    doc.close()

    full_text = "\n".join(pages)
    print(f"[1/4] Extracted text from {page_count} pages ({len(full_text)} chars)")
    return full_text


def clean_text(text: str) -> str:
    # 1. Fix hyphenated line breaks (Hebrew compounds: join without space)
    text = re.sub(r'-\n', '', text)
    # 2. Remove lines that are only digits (page numbers)
    text = re.sub(r'^\d+\s*$', '', text, flags=re.MULTILINE)
    # 3. Remove footnote/reference markers: [1], (1), superscripts ¹²³…
    text = re.sub(r'\[\d+\]|\(\d+\)|[¹²³⁴⁵⁶⁷⁸⁹⁰]+', '', text)
    # 4. Remove URLs
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # 5. Remove bullet/list markers at line start
    text = re.sub(r'^[•·\*]\s*', '', text, flags=re.MULTILINE)
    # 6. Replace common symbols
    for sym, rep in {'©': '', '®': '', '™': '', '→': '', '←': '',
                     '–': ',', '—': ',', '…': '.'}.items():
        text = text.replace(sym, rep)
    # 7. Collapse whitespace per line (trim leading/trailing spaces on each line)
    text = '\n'.join(line.strip() for line in text.splitlines())
    # 8. Remove standalone quote marks on their own line
    text = re.sub(r'^["\u201c\u201d\u2018\u2019]+$', '', text, flags=re.MULTILINE)
    # 9. Remove leading colons (RTL artifact: colon appears at start instead of end)
    text = re.sub(r'^:\s*', '', text, flags=re.MULTILINE)
    # 10. Fix chapter/section notation: פרק5 → פרק 5
    text = re.sub(r'([\u0590-\u05FF])(\d)', r'\1 \2', text)
    # 11. Fix Hebrew ordinal prefix: ה21 → ה-21 (so TTS reads "ה-עשרים ואחת")
    text = re.sub(r'\bה(\d+)', r'ה-\1', text)
    # 12. Join broken lines — lines that don't end with sentence punctuation
    #     are continuation of the previous line, not new paragraphs
    lines = text.splitlines()
    joined: list[str] = []
    sentence_end = re.compile(r'[.?!:;"\u201d\u2019]$')
    for line in lines:
        if not line:
            joined.append('')
        elif joined and joined[-1] and not sentence_end.search(joined[-1]):
            joined[-1] = joined[-1] + ' ' + line
        else:
            joined.append(line)
    text = '\n'.join(joined)
    # 13. Collapse 3+ newlines to paragraph break
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 14. Ensure paragraphs end with punctuation so TTS pauses between them
    text = re.sub(r'([^\.\?\!\,\:\;])\n\n', r'\1.\n\n', text)
    return text.strip()


def _split_by_sentences(text: str) -> list[str]:
    """Split text on sentence-ending punctuation, keeping the delimiter."""
    parts = re.split(r'(?<=[.?!])\s+', text.strip())
    return [p for p in parts if p.strip()]


def segment_text(full_text: str) -> list[str]:
    """
    Split text into chunks that fit within MAX_BYTES.
    Strategy: paragraphs → sentences → hard split.
    """
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', full_text) if p.strip()]
    segments: list[str] = []

    for para in paragraphs:
        if len(para.encode("utf-8")) <= MAX_BYTES:
            segments.append(para)
            continue

        # Paragraph too large — split by sentences
        sentences = _split_by_sentences(para)
        current = ""
        for sentence in sentences:
            candidate = (current + " " + sentence).strip() if current else sentence
            if len(candidate.encode("utf-8")) <= MAX_BYTES:
                current = candidate
            else:
                if current:
                    segments.append(current)
                # If a single sentence is still too large, hard-split it
                if len(sentence.encode("utf-8")) > MAX_BYTES:
                    encoded = sentence.encode("utf-8")
                    for i in range(0, len(encoded), MAX_BYTES):
                        segments.append(encoded[i:i + MAX_BYTES].decode("utf-8", errors="ignore"))
                    current = ""
                else:
                    current = sentence
        if current:
            segments.append(current)

    print(f"[2/4] Split into {len(segments)} segments")
    return segments


def save_segments(segments: list[str], output_path: Path) -> None:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(f"=== Segment {i} ===")
        lines.append(seg)
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[3/4] Segments saved to: {output_path}")


def synthesize_segments(segments: list[str]) -> list[bytes]:
    try:
        from google.cloud import texttospeech
    except ImportError:
        sys.exit("ERROR: google-cloud-texttospeech not installed. Run: pip install google-cloud-texttospeech")

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not Path(creds_path).exists():
        sys.exit(
            "ERROR: Google Cloud credentials not found.\n"
            "Set the environment variable:\n"
            "  Windows: $env:GOOGLE_APPLICATION_CREDENTIALS = 'C:\\path\\to\\key.json'\n"
            "  or add it permanently in System Environment Variables."
        )

    try:
        client = texttospeech.TextToSpeechClient()
    except Exception as e:
        sys.exit(f"ERROR: Could not create TTS client: {e}")

    voice = texttospeech.VoiceSelectionParams(
        language_code=VOICE_LANGUAGE,
        name=VOICE_NAME,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    )

    audio_chunks: list[bytes] = []
    total = len(segments)

    for i, segment in enumerate(segments, start=1):
        print(f"  TTS segment {i}/{total} ({len(segment.encode('utf-8'))} bytes)...")
        try:
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=segment),
                voice=voice,
                audio_config=audio_config,
            )
            audio_chunks.append(response.audio_content)
        except Exception as e:
            sys.exit(f"ERROR: TTS failed on segment {i}: {e}")

    return audio_chunks


def concatenate_audio(audio_chunks: list[bytes], output_path: Path) -> None:
    try:
        from pydub import AudioSegment
    except ImportError:
        sys.exit("ERROR: pydub not installed. Run: pip install pydub")

    combined = AudioSegment.empty()
    for chunk in audio_chunks:
        segment = AudioSegment.from_mp3(io.BytesIO(chunk))
        combined += segment

    combined.export(str(output_path), format="mp3")
    duration_sec = len(combined) / 1000
    print(f"[4/4] Audio saved to: {output_path}  ({duration_sec:.1f}s)")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python pdf_to_speech.py <path/to/file.pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        sys.exit(f"ERROR: File not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        sys.exit(f"ERROR: Expected a .pdf file, got: {pdf_path.suffix}")

    output_dir = pdf_path.parent
    stem = pdf_path.stem
    segments_path = output_dir / f"{stem}_segments.txt"
    audio_path = output_dir / f"{stem}_speech.mp3"

    print(f"=== PDF to Speech: {pdf_path.name} ===")

    full_text = extract_text(pdf_path)
    full_text = clean_text(full_text)
    segments = segment_text(full_text)
    save_segments(segments, segments_path)

    print(f"[4/4] Converting {len(segments)} segments to speech...")
    audio_chunks = synthesize_segments(segments)
    concatenate_audio(audio_chunks, audio_path)

    print("\nDone!")
    print(f"  Segments: {segments_path}")
    print(f"  Audio:    {audio_path}")


if __name__ == "__main__":
    main()
