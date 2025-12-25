#!/usr/bin/env python3
import os, re, sys, json, subprocess

MEDIA_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v")

# ---------- ffprobe ----------
def run_ffprobe(file):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,width,height,channels,channel_layout,color_primaries,color_transfer,color_space:stream_tags=language,title,handler_name",
        "-of", "json", file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"streams": []}

# Duration-only ffprobe for validation
def get_duration_ms(file):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        data = json.loads(result.stdout)
        dur = float(data.get("format", {}).get("duration", "0"))
        return int(round(dur * 1000))
    except Exception:
        return None

# ---------- helpers ----------
def is_english(lang):
    return lang and lang.lower() in {"en", "eng", "english"}

LAYOUT_TO_COUNT = {
    "mono":1,"1.0":1,
    "stereo":2,"2.0":2,
    "2.1":3,"3.0":3,"3.1":4,
    "4.0":4,"4.1":5,
    "5.0":5,"5.1":6,
    "6.1":7,"7.1":8,
    "7.1(wide)":8
}

def channel_count(s):
    ch = s.get("channels")
    if isinstance(ch, int) and ch > 0:
        return ch
    layout = (s.get("channel_layout") or "").lower()
    if layout in LAYOUT_TO_COUNT:
        return LAYOUT_TO_COUNT[layout]
    m = re.match(r"(\d+)\.(\d+)", layout)
    if m:
        return int(m.group(1)) + int(m.group(2))
    return "?"

def detect_hdr(s):
    prim = (s.get("color_primaries") or "").lower()
    trans = (s.get("color_transfer") or "").lower()
    codec = (s.get("codec_name") or "").lower()
    if "bt2020" in prim or "smpte2084" in trans:
        return "HDR10"
    if "hlg" in trans:
        return "HLG"
    if "dvhe" in codec or "dvh1" in codec:
        return "Dolby Vision"
    return "SDR"

def detect_audio_extension(s):
    codec = (s.get("codec_name") or "").lower()
    tags = s.get("tags") or {}
    title = (tags.get("title") or "").lower()
    handler = (tags.get("handler_name") or "").lower()
    has_atmos = "atmos" in title or "atmos" in handler
    has_dtsx  = "dts:x" in title or "dts:x" in handler or "dtsx" in title or "dtsx" in handler
    return codec, has_atmos, has_dtsx

def codec_label(s):
    codec, has_atmos, has_dtsx = detect_audio_extension(s)
    if codec == "truehd":
        return "Dolby Atmos" if has_atmos else "Dolby TrueHD"
    if codec == "eac3":
        return "Dolby Atmos" if has_atmos else "Dolby Digital Plus (E-AC3)"
    if codec.startswith("dts"):
        return "DTS:X" if has_dtsx else "DTS / DTS-HD"
    if codec == "ac3":
        return "AC3"
    if codec == "aac":
        return "AAC"
    return codec.upper()

def audio_label(s):
    lang = (s.get("tags") or {}).get("language", "Unknown")
    ch = channel_count(s)
    return f"{codec_label(s)}-{ch} ({lang})"

def summarize(streams):
    hdr = "SDR"
    audio_lines = []
    subs_langs = []
    for s in streams.get("streams", []):
        st = s.get("codec_type")
        if st == "video":
            hdr = detect_hdr(s)
        elif st == "audio":
            audio_lines.append(audio_label(s))
        elif st == "subtitle":
            subs_langs.append((s.get("tags") or {}).get("language", "Unknown"))
    return (
        f"HDR: {hdr}\n"
        f"Audio streams ({len(audio_lines)}): {', '.join(audio_lines)}\n"
        f"Subtitle streams ({len(subs_langs)}): {', '.join(subs_langs)}"
    )

def wait_for_key():
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch.lower()
    except ImportError:
        import msvcrt
        return msvcrt.getch().decode().lower()

# ---------- rule engine ----------
def apply_rules(streams):
    """
    Rules:
      1. Keep all English TrueHD/EAC3/DTS streams (they can carry 3D audio).
      2. Remove all AAC streams.
      3. Remove all non-English audio streams.
      4. Remove all non-English subtitle streams.
      5. Remove all 2-channel English streams if there’s at least one English >2ch.
      6. If there isn’t at least one English AC3 3+ channel audio stream, create one.
    """
    st = streams.get("streams", [])
    audio_streams = [s for s in st if s.get("codec_type") == "audio"]
    subs_streams  = [s for s in st if s.get("codec_type") == "subtitle"]

    notes = []
    kept_audio = []

    for s in audio_streams:
        lang = (s.get("tags") or {}).get("language", "")
        if not is_english(lang):
            continue

        codec, has_atmos, has_dtsx = detect_audio_extension(s)

        # Keep all TrueHD/EAC3/DTS because they *can* contain 3D audio
        if codec in {"truehd", "eac3", "dts"}:
            kept_audio.append(s)
            continue

        # Drop AAC always
        if codec == "aac":
            notes.append(f"Drop {audio_label(s)} (AAC)")
            continue

        # Keep AC3 (we'll enforce 3+ch via rule #6)
        if codec == "ac3":
            kept_audio.append(s)
            continue

        # Other codecs are dropped
        notes.append(f"Drop {audio_label(s)} (unsupported codec for this rule set)")

    # Remove 2ch if >2ch exists
    has_gt2 = any(isinstance(channel_count(s), int) and channel_count(s) > 2 for s in kept_audio)
    if has_gt2:
        before = len(kept_audio)
        kept_audio = [s for s in kept_audio if isinstance(channel_count(s), int) and channel_count(s) > 2]
        pruned = before - len(kept_audio)
        if pruned:
            notes.append(f"Removed {pruned} 2-channel English streams")

    # Ensure AC3 3+ exists
    has_ac3_3plus = any(
        (s.get("codec_name") or "").lower() == "ac3" and
        isinstance(channel_count(s), int) and channel_count(s) >= 3
        for s in kept_audio
    )

    audio_new_ac3 = None
    if not has_ac3_3plus:
        english_candidates = [s for s in audio_streams if is_english((s.get("tags") or {}).get("language", ""))]
        if english_candidates:
            def score(s):
                codec, has_atmos, has_dtsx = detect_audio_extension(s)
                ch = channel_count(s)
                ext = 100 if (has_atmos or has_dtsx) else 0
                base = 50 if codec in {"truehd", "eac3", "dts"} else 10
                chs = ch if isinstance(ch, int) else 0
                return ext + base + chs
            source = max(english_candidates, key=score)
            audio_new_ac3 = {
                "source_audio_pos": position_within_type(audio_streams, source),
                "out_channels": 6,
                "bitrate": "640k",
                "desc": f"Create AC3-6 (eng) from {audio_label(source)}"
            }
            notes.append(audio_new_ac3["desc"])
        else:
            notes.append("No English source available to create AC3")

    subs_keep = [s for s in subs_streams if is_english((s.get("tags") or {}).get("language", ""))]

    plan = {
        "video_copy": True,
        "audio_keep": [
            {"pos": position_within_type(audio_streams, s), "desc": f"Keep {audio_label(s)}"}
            for s in kept_audio
        ],
        "audio_new_ac3": audio_new_ac3,
        "subs_keep": [
            {"pos": position_within_type(subs_streams, s), "desc": "Keep English subtitle"}
            for s in subs_keep
        ],
        "notes": notes
    }
    return plan

def position_within_type(group, stream_obj):
    for i, s in enumerate(group):
        if s is stream_obj:
            return i
    return 0

# ---------- ffmpeg command builder ----------
def build_ffmpeg_command(infile, outfile, plan):
    cmd = ["ffmpeg", "-y", "-i", infile]

    if plan["video_copy"]:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]

    for i, a in enumerate(plan["audio_keep"]):
        cmd += ["-map", f"0:a:{a['pos']}", f"-c:a:{i}", "copy"]

    next_a = len(plan["audio_keep"])
    if plan["audio_new_ac3"]:
        src = plan["audio_new_ac3"]["source_audio_pos"]
        ch = plan["audio_new_ac3"]["out_channels"]
        br = plan["audio_new_ac3"]["bitrate"]
        cmd += [
            "-map", f"0:a:{src}",
            f"-c:a:{next_a}", "ac3",
            f"-b:a:{next_a}", br,
            f"-ac:a:{next_a}", str(ch)
        ]

    for i, s in enumerate(plan["subs_keep"]):
        cmd += ["-map", f"0:s:{s['pos']}", f"-c:s:{i}", "copy"]

    cmd += ["-map_metadata", "0", "-map_chapters", "0", outfile]
    return cmd

def explain_command(cmd, plan):
    lines = []
    lines.append("-y: overwrite the OUTPUT file if it already exists (input/original is never overwritten).")
    lines.append("-i INPUT: source file to read from.")
    if plan["video_copy"]:
        lines.append("-map 0:v:0 -c:v copy: keep the primary video stream as-is (no reencode).")
    if plan["audio_keep"]:
        lines.append("Audio streams kept (copied):")
        for i, a in enumerate(plan["audio_keep"]):
            lines.append(f"  - -map 0:a:{a['pos']} -c:a:{i} copy → {a['desc']}")
    if plan["audio_new_ac3"]:
        i = len(plan["audio_keep"])
        src = plan["audio_new_ac3"]["source_audio_pos"]
        lines.append("AC3 creation:")
        lines.append(
            f"  - -map 0:a:{src} -c:a:{i} ac3 -b:a:{i} {plan['audio_new_ac3']['bitrate']} "
            f"-ac:a:{i} {plan['audio_new_ac3']['out_channels']} "
            f"→ New AC3-6 (eng) track."
        )
    if plan["subs_keep"]:
        lines.append("Subtitles kept (copied):")
        for i, s in enumerate(plan["subs_keep"]):
            lines.append(f"  - -map 0:s:{s['pos']} -c:s:{i} copy → {s['desc']}")
    lines.append("-map_metadata 0 -map_chapters 0: preserve original metadata and chapters.")
    lines.append("OUTPUT.mkv: final Matroska output file.")
    return "\n".join(lines)

def summarize_resulting_plan(streams, plan):
    audio_streams = [s for s in streams.get("streams", []) if s.get("codec_type") == "audio"]
    subs_streams  = [s for s in streams.get("streams", []) if s.get("codec_type") == "subtitle"]
    out = []
    out.append("Video: copy original.")
    if plan["audio_keep"]:
        out.append("Audio kept:")
        for a in plan["audio_keep"]:
            s = audio_streams[a["pos"]]
            out.append(f"  - {audio_label(s)}")
    if plan["audio_new_ac3"]:
        src = plan["audio_new_ac3"]["source_audio_pos"]
        s = audio_streams[src]
        out.append("Audio added:")
        out.append(f"  - New AC3-6 (eng) from {audio_label(s)}")
    if plan["subs_keep"]:
        out.append("Subtitles kept:")
        for splan in plan["subs_keep"]:
            s = subs_streams[splan["pos"]]
            lang = (s.get("tags") or {}).get("language", "Unknown")
            out.append(f"  - English subtitle (lang={lang})")
    else:
        out.append("Subtitles kept: none (non-English removed).")
    if plan["notes"]:
        out.append("Decisions:")
        for n in plan["notes"]:
            out.append(f"  - {n}")
    return "\n".join(out)

def quote_arg(a):
    return f"'{a}'" if re.search(r'\s', a) else a

# ---------- validation ----------
def validate_output_file(input_file, output_file):
    """
    Hybrid validation:
      1) Compare exact duration in ms via ffprobe.
         - If identical → PASS.
         - If any difference → run decode test.
      2) Decode test: ffmpeg -v error -i output -f null -
         - If exit code 0 and no errors → PASS.
         - Otherwise → FAIL.
    Returns (passed: bool, message: str)
    """
    d_in = get_duration_ms(input_file)
    d_out = get_duration_ms(output_file)

    if d_in is not None and d_out is not None and d_in == d_out:
        return True, f"Durations match exactly ({d_in} ms)."

    # Durations differ or missing → decode test
    cmd = ["ffmpeg", "-v", "error", "-i", output_file, "-f", "null", "-"]
    print("Running ffmpeg decode test (may take up to 90 seconds)...")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
        if proc.returncode == 0 and not proc.stderr.strip():
            return True, "Decode test passed with no errors (duration mismatch accepted)."
        else:
            return False, "Decode test reported errors or non-zero exit code."
    except subprocess.TimeoutExpired:
        return False, "Decode test timed out (validation failed)."
    if proc.returncode == 0 and not proc.stderr.strip():
        return True, "Decode test passed with no errors (duration mismatch accepted)."
    else:
        return False, "Decode test reported errors or non-zero exit code."
def process_file(file):
    streams = run_ffprobe(file)
    print(f"\nAnalyzing: {os.path.basename(file)}")
    print("=== Summary ===")
    print(summarize(streams))
    print()

    plan = apply_rules(streams)

    # Auto-skip if no changes would be made
    if not plan["notes"] and plan["audio_new_ac3"] is None:
        print(f"No changes needed for '{os.path.basename(file)}'. Skipping.")
        return

    outfile = os.path.splitext(file)[0] + ".reencoded.mkv"
    cmd = build_ffmpeg_command(file, outfile, plan)

    print("=== ffmpeg command (dry-run) ===")
    print(" ".join(quote_arg(a) for a in cmd))
    print()
    print("=== Command explanation ===")
    print(explain_command(cmd, plan))
    print()
    print("=== Resulting streams (dry-run) ===")
    print(summarize_resulting_plan(streams, plan))
    print("\n" + "-"*72)

    while True:
        print("Press N for next file, D/R to reencode, Q/X to quit.")
        choice = wait_for_key()
        if choice == "n":
            return
        if choice in ("q", "x"):
            sys.exit(0)
        if choice in ("d", "r"):
            break

    # At this point, user chose to actually reencode
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mediainfo_path = os.path.join(script_dir, "mediainfo.py")
    filename_only = os.path.basename(file)

    # B4: run mediainfo.py on input file (by filename; mediainfo.py does the directory search)
    try:
        b4 = subprocess.check_output(
            ["python3", mediainfo_path, filename_only],
            text=True
        )
    except subprocess.CalledProcessError as e:
        b4 = f"(mediainfo.py failed on input file: {e})"

    # Perform reencode (may be retried)
    while True:
        if os.path.exists(outfile):
            os.remove(outfile)

        print(f"\nReencoding '{file}' → '{outfile}' ...")
        proc = subprocess.run(cmd, text=True)
        if proc.returncode != 0 or not os.path.exists(outfile):
            print("ffmpeg reported an error or output file was not created.")
            print("The output file failed validation checks.")
            print("Retry the reencode operation? (Y/N)")
            while True:
                ch = wait_for_key()
                if ch == "y":
                    break  # retry loop
                if ch == "n":
                    return  # skip to next file
            continue  # retry reencode

        # AFTR: run mediainfo.py on output file (by its new filename)
        out_name_only = os.path.basename(outfile)
        try:
            aftr = subprocess.check_output(
                ["python3", mediainfo_path, out_name_only],
                text=True
            )
        except subprocess.CalledProcessError as e:
            aftr = f"(mediainfo.py failed on output file: {e})"

        print(f"\nMedia Info for {filename_only} BEFORE reencode operation:")
        print(b4)
        print("========")
        print(f"Media Info for {out_name_only} AFTER reencode operation:")
        print(aftr)

        # Validation
        passed, msg = validate_output_file(file, outfile)
        if passed:
            print("\nThe output file has passed validation checks.")
            print(f"Details: {msg}")
            print(f"Accept reencode and replace '{file}' with '{outfile}'? (Y/N)")
            while True:
                ch = wait_for_key()
                if ch == "y":
                    # Replace original with reencoded (atomic overwrite)
                    os.replace(outfile, file)
                    print("Original file has been replaced with the reencoded file.")
                    return
                if ch == "n":
                    # Keep original, discard reencode
                    os.remove(outfile)
                    print("Reencode discarded; original file kept.")
                    return
        else:
            print("\nThe output file failed validation checks.")
            print(f"Details: {msg}")
            print("Retry the reencode operation? (Y/N)")
            while True:
                ch = wait_for_key()
                if ch == "y":
                    break  # retry reencode
                if ch == "n":
                    # Validation failed and user declined retry; discard output
                    if os.path.exists(outfile):
                        os.remove(outfile)
                    print("Reencode discarded; moving to next file.")
                    return
        # If we get here after a failed validation and user chose retry, loop continues

def main(directory):
    files = sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.lower().endswith(MEDIA_EXTENSIONS)
    )
    if not files:
        print("No media files found in:", directory)
        return
    for file in files:
        process_file(file)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: mediainfo_reencode_dryrun.py /path/to/media")
        sys.exit(1)
    main(sys.argv[1])
