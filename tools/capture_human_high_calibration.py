        ips = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, None)}
        return bool(ips) and all(ip.is_global for ip in ips)
    except (OSError, ValueError):
        return False

def capture(page, url, output, width, height):
    page.set_viewport_size({"width": width, "height": height})
    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(900)
    try:
        page.evaluate("""async () => { for (const i of document.images) i.loading='eager'; await Promise.all(Array.from(document.images, i=>i.decode().catch(()=>{}))); window.scrollTo(0,document.body.scrollHeight); await new Promise(r=>setTimeout(r,250)); window.scrollTo(0,0); }""")
    except Exception:
        pass
    try:
        page.screenshot(path=str(output), full_page=True, animations="disabled", timeout=30000)
        mode = "full_page"
    except Exception:
        page.screenshot(path=str(output), full_page=False, animations="disabled", timeout=15000)
        mode = "viewport_fallback"
    return {"status":"saved", "path":output.relative_to(Path.cwd()).as_posix(), "mode":mode,
            "http_status":response.status if response else None,
            "viewport":{"width":width,"height":height}}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--targets-csv",type=Path,required=True)
    parser.add_argument("--run-dir",type=Path,required=True)
    args=parser.parse_args()
    run_dir=args.run_dir.resolve(); shots=run_dir/"screenshots"; shots.mkdir(parents=True,exist_ok=True)
    with args.targets_csv.open(encoding="utf-8-sig",newline="") as stream:
        targets=list(csv.DictReader(stream))
    if not targets: raise ValueError("Calibration CSV is empty")
    rows=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True); context=browser.new_context(); page=context.new_page(); mobile_context=browser.new_context(viewport={"width":390,"height":844},device_scale_factor=1,is_mobile=True,has_touch=True); mobile_page=mobile_context.new_page()
        for n,target in enumerate(targets,1):
            url=(target.get("website") or "").strip(); parts=urlsplit(url); host=parts.hostname or ""
            record={"candidate_id":"", "business_name":(target.get("business") or "").strip(),
                    "category":"Human-reviewed High calibration cohort",
                    "configured_location":(target.get("location") or "").strip(), "region":"Colorado",
                    "canonical_url":url,"final_url":url,"domain":host,"site_check_status":"error","screenshots":{}}
            try:
                if parts.scheme not in {"http","https"} or not host or parts.username or parts.password or not is_public(host):
                    raise ValueError("Target URL must use HTTP(S) and resolve to a public host")
                slug=re.sub("[^A-Za-z0-9]+","_",host).strip("_").lower()[:100]
                record["candidate_id"]=f"cal-high-{n:03d}-{slug}"
                desktop=shots/f"{record['candidate_id']}_desktop.png"
                mobile=shots/f"{record['candidate_id']}_mobile.png"
                record["screenshots"]["desktop"]=capture(page,url,desktop,1440,1000)
                final_url=page.url; final_host=urlsplit(final_url).hostname or host
                if not is_public(final_host): raise ValueError("Redirected host is not public")
                record["site_title"]=page.title(); record["final_url"]=final_url; record["domain"]=final_host
                record["screenshots"]["mobile"]=capture(mobile_page,final_url,mobile,390,844)
                record["site_check_status"]="loaded"
                record["human_review"]={"decision":"high","basis":"private calibration cohort; label is not sent to visual model"}
            except Exception as exc:
                record["capture_error"]=f"{type(exc).__name__}: {exc}"[:700]
            rows.append(record)
        mobile_context.close(); context.close(); browser.close()
    (run_dir/"CANDIDATE_LEDGER.jsonl").write_text("".join(json.dumps(row,ensure_ascii=False)+chr(10) for row in rows),encoding="utf-8")
    manifest={"run_key":run_dir.name,"run_type":"human_high_calibration_capture",
              "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "target_count":len(rows),"capture_method":"Playwright Chromium, desktop/mobile full-page screenshots",
              "human_labels_are_not_sent_to_visual_model":True}
    (run_dir/"RUN_MANIFEST.json").write_text(json.dumps(manifest,indent=2)+chr(10),encoding="utf-8")
    print(json.dumps({"target_count":len(rows),"loaded":sum(r["site_check_status"]=="loaded" for r in rows),
                      "failed":sum(r["site_check_status"]!="loaded" for r in rows),
                      "candidate_ids":[r["candidate_id"] for r in rows]},indent=2))
if __name__=="__main__": main()
