import sys, os

new_diag_code = """    # ── 7. Diagnostics ───────────────────────────────────────────────────
    with tab_diag:
        st.subheader("📥 Download Bot Logs")
        st.caption("Filter historical logs by date/time and choose your log source.")
        
        log_source = st.radio("Log Source", ["Bot Internal Files (logs/bot.log)", "System Service (journalctl -u tradingbot)"], horizontal=True)

        today = datetime.date.today()
        d1, d2 = st.columns(2)
        start_date = d1.date_input("Start Date", value=today - datetime.timedelta(days=2))
        end_date   = d2.date_input("End Date",   value=today)
        t1c, t2c = st.columns(2)
        start_time = t1c.time_input("Start Time", value=datetime.time(0, 0))
        end_time   = t2c.time_input("End Time",   value=datetime.time(23, 59, 59))

        if st.button("🔍 Filter & Prepare Download", use_container_width=True):
            start_dt = datetime.datetime.combine(start_date, start_time)
            end_dt   = datetime.datetime.combine(end_date,   end_time)
            fname = f"bot_logs_{start_date}_{start_time.strftime('%H%M')}_to_{end_date}_{end_time.strftime('%H%M')}.txt"
            matched_text = ""

            if "Internal" in log_source:
                log_dir = Path("logs").absolute()
                matched = []
                if log_dir.exists() and log_dir.is_dir():
                    files_to_scan = sorted(log_dir.glob("bot.log*"))
                    for lpath in files_to_scan:
                        try:
                            with lpath.open("r", encoding="utf-8", errors="replace") as fh:
                                for line in fh:
                                    try:
                                        ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                                        if start_dt <= ts <= end_dt:
                                            matched.append(line)
                                    except ValueError:
                                        if matched:
                                            matched.append(line)
                        except Exception as e:
                            st.warning(f"Could not read {lpath.name}: {e}")
                if matched:
                    matched_text = "".join(matched)
            else:
                import subprocess
                since_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                until_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    res = subprocess.run(
                        ["journalctl", "-u", "tradingbot", "--since", since_str, "--until", until_str, "--no-pager"],
                        capture_output=True, text=True, check=True
                    )
                    matched_text = res.stdout
                except Exception as e:
                    st.error(f"Failed to fetch system logs: {e}")

            if matched_text.strip():
                st.success(f"Log retrieval successful ({len(matched_text.splitlines())} lines).")
                st.download_button("⬇️ Download Logs", data=matched_text, file_name=fname, mime="text/plain", use_container_width=True)
            else:
                st.warning("No entries in the selected range.")

        st.markdown("---")
        
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            log_path = Path("logs/bot.log").absolute()
            if log_path.exists():
                size_kb = log_path.stat().st_size / 1024
                mtime   = datetime.datetime.fromtimestamp(log_path.stat().st_mtime)
                st.info(f"📄 `bot.log` — **{size_kb:.1f} KB** | Mod: **{mtime:%Y-%m-%d %H:%M:%S}**")
                with st.expander("Live Bot Feed (Internal)"):
                    try:
                        with log_path.open("r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                            log_content = "".join(reversed(lines[-30:]))
                            st.text_area("Recent Bot Logs", log_content, height=300, label_visibility="collapsed")
                    except Exception as e:
                        st.error(f"Error reading live feed: {e}")
            else:
                st.info("Log file not found yet.")

        with d_col2:
            st.info("🖥️ `tradingbot` Service Journal")
            with st.expander("Live Systemd Feed"):
                try:
                    import subprocess
                    res = subprocess.run(["journalctl", "-u", "tradingbot", "-n", "30", "--no-pager"],
                                         capture_output=True, text=True)
                    sys_content = "".join(reversed(res.stdout.splitlines(True)))
                    st.text_area("Recent System Logs", sys_content, height=300, label_visibility="collapsed")
                except Exception as e:
                    st.error(f"Could not read journalctl: {e}")
        
        if st.button("Refresh Feeds", use_container_width=True): st.rerun()
"""

target_file = 'c:/Users/Conor/Documents/GitHub/trdbt/app.py'

with open(target_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith('    # ── 7. Diagnostics ───────────────────────────────────────────────────'):
        start_idx = i
    if line.startswith('# ──────────────────────────────────────────────────────────────────────────'):
        if start_idx != -1 and i > start_idx and i + 1 < len(lines) and '⚙ Cog button' in lines[i+1]:
            end_idx = i
            break

if start_idx != -1 and end_idx != -1:
    new_lines = lines[:start_idx] + [new_diag_code + "\n"] + lines[end_idx:]
    with open(target_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("SUCCESS: Rewrote Diagnostics in app.py")
else:
    print(f"FAILED: start_idx={start_idx}, end_idx={end_idx}")
