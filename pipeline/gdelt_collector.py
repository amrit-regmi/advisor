"""
GDELT GKG collector with 30-day trend analysis.
Downloads all 96 daily files, aggregates mentions per entity,
computes mention_velocity and tone_trajectory signals.
Uses FinBERT for financial sentiment scoring; falls back to GDELT column 15.
"""
import requests
import zipfile
import io
import sys
import time
from datetime import date, timedelta, datetime
from collections import defaultdict

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, query, log, track_api_call

GDELT_MASTER = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
GDELT_LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

# GKG column indices (tab-separated)
GKG_DATE = 0
GKG_THEMES = 7
GKG_TONE = 15   # tone field: tone,pos%,neg%,polarity,activity,self_ref

# FinBERT — loaded once, reused across all calls
_FINBERT_PIPELINE = None

def _get_finbert():
    global _FINBERT_PIPELINE
    if _FINBERT_PIPELINE is not None:
        return _FINBERT_PIPELINE
    try:
        from transformers import pipeline
        # local_files_only prevents the hub-version-check request that
        # generates "unauthenticated requests" warnings when HF_TOKEN is unset.
        _FINBERT_PIPELINE = pipeline(
            'text-classification',
            model='ProsusAI/finbert',
            tokenizer='ProsusAI/finbert',
            cache_dir='/home/ubuntu/advisor/models/finbert',
            local_files_only=True,
            top_k=None,
        )
        log('INFO', 'gdelt', 'FinBERT loaded successfully')
    except Exception as e:
        log('WARNING', 'gdelt', f'FinBERT load failed, will use GDELT col-15 tone: {e}')
        _FINBERT_PIPELINE = False  # don't retry
    return _FINBERT_PIPELINE


def _finbert_tone(text):
    """
    Score a text snippet with FinBERT.
    Returns (net_score, confidence) where:
      net_score  = positive_prob - negative_prob, range [-1, +1]
      confidence = max class probability (the model's actual certainty, range [0, 1])
    Returns (None, None) if FinBERT unavailable.
    """
    pipe = _get_finbert()
    if not pipe:
        return None, None
    try:
        result = pipe(text[:512], truncation=True)
        scores = {r['label']: r['score'] for r in result[0]}
        net = scores.get('positive', 0) - scores.get('negative', 0)
        confidence = max(scores.values())  # actual model certainty, not net score
        return net, confidence
    except Exception:
        return None, None

FINANCIAL_THEMES = [
    'TAX_FNCACT', 'ECON_BANKRUPTCY', 'HEALTH_PANDEMIC', 'ENV_OIL',
    'MILITARY', 'ELECTION', 'SANCTION', 'UNGP_BUSINESS',
    'HEALTH_DISEASE', 'MED_MEDICATION', 'TECH', 'SCIENCE_COMPUTING',
    'WB_696_FINANCE', 'ECON_TRADE', 'WB_2105_TRANSPORT',
    'ENV_COAL', 'ENV_NUCLEAR', 'ECON_TAXATION', 'UNGP_PEACESEC',
]


def _get_master_urls_for_date(target_date):
    """Return list of GKG zip URLs for a given date from master file list."""
    date_str = target_date.strftime('%Y%m%d')
    try:
        log('INFO', 'gdelt', f'Fetching master file list for {date_str}...')
        resp = requests.get(GDELT_MASTER, timeout=120, stream=True)
        urls = []
        for line in resp.iter_lines():
            line = line.decode('utf-8', errors='ignore').strip()
            parts = line.split(' ')
            if len(parts) == 3:
                url = parts[2]
                if date_str in url and 'gkg.csv.zip' in url:
                    urls.append(url)
        log('INFO', 'gdelt', f'Found {len(urls)} GKG files for {date_str}')
        return urls
    except Exception as e:
        log('ERROR', 'gdelt', f'Master file fetch failed: {e}')
        return []


def _get_today_urls():
    """Get the 3 most recent file URLs from lastupdate.txt."""
    try:
        resp = requests.get(GDELT_LASTUPDATE, timeout=30)
        urls = []
        for line in resp.text.strip().split('\n'):
            parts = line.strip().split(' ')
            if len(parts) == 3:
                url = parts[2]
                if 'gkg.csv.zip' in url:
                    urls.append(url)
        return urls
    except Exception as e:
        log('ERROR', 'gdelt', f'lastupdate fetch failed: {e}')
        return []


def _download_and_parse_gkg(url, search_terms):
    """Download one GKG zip and return list of (gdelt_tone, themes) for matching rows."""
    try:
        track_api_call('gdelt')
        resp = requests.get(url, timeout=60)
        if resp.status_code != 200:
            return []
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        csv_name = z.namelist()[0]
        with z.open(csv_name) as f:
            content = f.read().decode('latin-1', errors='ignore')

        results = []
        content_lower = content.lower()
        if not any(term in content_lower for term in search_terms):
            return []

        for line in content.split('\n'):
            line_lower = line.lower()
            if not any(term in line_lower for term in search_terms):
                continue
            parts = line.split('\t')
            if len(parts) <= GKG_TONE:
                continue
            try:
                tone_raw = parts[GKG_TONE].split(',')[0]
                gdelt_tone = float(tone_raw)
            except (ValueError, IndexError):
                gdelt_tone = 0.0
            themes = parts[GKG_THEMES] if len(parts) > GKG_THEMES else ''
            results.append((gdelt_tone, themes))
        return results
    except Exception:
        return []


def _aggregate_results(rows):
    """Summarise raw (gdelt_tone, themes) rows into sentiment dict."""
    if not rows:
        return {'article_count': 0, 'avg_tone': 0.0,
                'positive_count': 0, 'negative_count': 0, 'top_themes': ''}

    gdelt_tones = [r[0] for r in rows]
    theme_counts = defaultdict(int)
    for _, themes_str in rows:
        for theme in themes_str.split(';'):
            t = theme.strip().upper()
            if t and any(ft in t for ft in FINANCIAL_THEMES):
                theme_counts[t] += 1

    top_themes = ','.join(
        k for k, _ in sorted(theme_counts.items(), key=lambda x: -x[1])[:10]
    )

    # FinBERT scoring: run on aggregated themes text as financial text proxy
    finbert_score, finbert_conf_raw = None, None
    if top_themes:
        finbert_score, finbert_conf_raw = _finbert_tone(
            top_themes.replace(',', ' ').replace('_', ' ').lower()
        )

    if finbert_score is not None:
        # Scale FinBERT [-1,+1] to GDELT-like range (roughly [-10, +10])
        avg_tone = round(finbert_score * 10, 4)
    else:
        # Fall back to GDELT column 15 average
        avg_tone = round(sum(gdelt_tones) / len(gdelt_tones), 4)

    # Store FinBERT fields separately for downstream use
    finbert_label = None
    finbert_conf = None
    if finbert_score is not None:
        if finbert_score > 0.1:
            finbert_label = 'positive'
        elif finbert_score < -0.1:
            finbert_label = 'negative'
        else:
            finbert_label = 'neutral'
        # Use the model's actual max-class probability, not the net score
        finbert_conf = round(finbert_conf_raw, 4) if finbert_conf_raw is not None else None

    return {
        'article_count': len(rows),
        'avg_tone': avg_tone,
        'positive_count': sum(1 for t in gdelt_tones if t > 0),
        'negative_count': sum(1 for t in gdelt_tones if t < 0),
        'top_themes': top_themes,
        'finbert_sentiment_score': round(finbert_score, 4) if finbert_score is not None else None,
        'finbert_sentiment_label': finbert_label,
        'finbert_confidence': finbert_conf,
    }


def _already_have_date(ticker, target_date):
    rows = query(
        "SELECT id FROM news_sentiment WHERE ticker=%s AND date=%s",
        (ticker, target_date)
    )
    return len(rows) > 0


def _save_daily_sentiment(ticker, target_date, agg):
    execute("""
        INSERT INTO news_sentiment
            (ticker, date, article_count, avg_tone, positive_count,
             negative_count, top_themes,
             finbert_sentiment_score, finbert_sentiment_label, finbert_confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ticker, date) DO UPDATE SET
            article_count            = EXCLUDED.article_count,
            avg_tone                 = EXCLUDED.avg_tone,
            positive_count           = EXCLUDED.positive_count,
            negative_count           = EXCLUDED.negative_count,
            top_themes               = EXCLUDED.top_themes,
            finbert_sentiment_score  = EXCLUDED.finbert_sentiment_score,
            finbert_sentiment_label  = EXCLUDED.finbert_sentiment_label,
            finbert_confidence       = EXCLUDED.finbert_confidence
    """, (
        ticker, target_date,
        agg['article_count'], agg['avg_tone'],
        agg['positive_count'], agg['negative_count'],
        agg['top_themes'],
        agg.get('finbert_sentiment_score'),
        agg.get('finbert_sentiment_label'),
        agg.get('finbert_confidence'),
    ))


def _compute_and_save_trends(ticker):
    """Calculate mention_velocity and tone_trajectory from stored history."""
    rows = query("""
        SELECT date, article_count, avg_tone
        FROM news_sentiment
        WHERE ticker = %s
        ORDER BY date DESC
        LIMIT 35
    """, (ticker,))

    if len(rows) < 2:
        return

    today_row = rows[0]
    today_count = today_row['article_count'] or 0
    today_tone = float(today_row['avg_tone'] or 0)

    # 30-day rolling average (excluding today)
    hist = rows[1:]
    avg_30d = sum(r['article_count'] or 0 for r in hist) / len(hist) if hist else 1
    mention_velocity = round(today_count / max(avg_30d, 1), 4)

    # Tone trajectory: this week vs last week
    week1 = [float(r['avg_tone'] or 0) for r in rows[:7]]
    week2 = [float(r['avg_tone'] or 0) for r in rows[7:14]]
    tone_this_week = sum(week1) / len(week1) if week1 else 0
    tone_last_week = sum(week2) / len(week2) if week2 else 0
    tone_trajectory = round(tone_this_week - tone_last_week, 4)

    is_accelerating = mention_velocity > 2.0

    execute("""
        UPDATE news_sentiment
        SET mention_velocity = %s,
            tone_trajectory  = %s,
            is_accelerating  = %s
        WHERE ticker = %s AND date = %s
    """, (mention_velocity, tone_trajectory, is_accelerating,
          ticker, today_row['date']))

    status = 'ACCELERATING ↑' if is_accelerating else 'stable'
    log('INFO', 'gdelt',
        f'{ticker}: velocity={mention_velocity:.2f}x, '
        f'tone_traj={tone_trajectory:+.2f}, {status}')


def collect_day(target_date, watchlist, use_master=True):
    """Download all GKG files for target_date and aggregate per ticker."""
    if use_master:
        urls = _get_master_urls_for_date(target_date)
    else:
        urls = _get_today_urls()

    if not urls:
        log('WARNING', 'gdelt', f'No GKG URLs found for {target_date}')
        return

    log('INFO', 'gdelt', f'Processing {len(urls)} files for {target_date}')

    # Build per-ticker search terms
    ticker_terms = {}
    for ticker, company_name in watchlist.items():
        terms = set()
        for word in company_name.lower().split():
            if len(word) > 3:
                terms.add(word)
        terms.add(ticker.lower())
        ticker_terms[ticker] = list(terms)

    # Accumulate rows per ticker across all files
    ticker_rows = defaultdict(list)

    for i, url in enumerate(urls):
        all_terms = set()
        for terms in ticker_terms.values():
            all_terms.update(terms)

        rows_in_file = _download_and_parse_gkg(url, list(all_terms))

        if rows_in_file:
            # Re-attribute each row to the right ticker
            try:
                resp = requests.get(url, timeout=60)
                z = zipfile.ZipFile(io.BytesIO(resp.content))
                with z.open(z.namelist()[0]) as f:
                    content = f.read().decode('latin-1', errors='ignore')
                lines = content.split('\n')
                for ticker, terms in ticker_terms.items():
                    for line in lines:
                        line_lower = line.lower()
                        if any(t in line_lower for t in terms):
                            parts = line.split('\t')
                            if len(parts) > GKG_TONE:
                                try:
                                    tone = float(parts[GKG_TONE].split(',')[0])
                                    themes = parts[GKG_THEMES] if len(parts) > GKG_THEMES else ''
                                    ticker_rows[ticker].append((tone, themes))
                                except (ValueError, IndexError):
                                    pass
            except Exception:
                pass

        if (i + 1) % 10 == 0:
            log('INFO', 'gdelt', f'  Processed {i+1}/{len(urls)} files...')
        time.sleep(0.1)

    # Save aggregated results
    for ticker in watchlist:
        rows = ticker_rows.get(ticker, [])
        agg = _aggregate_results(rows)
        _save_daily_sentiment(ticker, target_date, agg)
        log('INFO', 'gdelt',
            f'{ticker} {target_date}: {agg["article_count"]} articles, '
            f'tone {agg["avg_tone"]:+.2f}')


def backfill_30_days(watchlist):
    """Download last 30 days of history if not already cached."""
    today = date.today()
    log('INFO', 'gdelt', 'Starting 30-day historical backfill...')
    for days_ago in range(30, 0, -1):
        target = today - timedelta(days=days_ago)
        # Check if we already have data for any ticker on this date
        sample_ticker = next(iter(watchlist))
        if _already_have_date(sample_ticker, target):
            log('INFO', 'gdelt', f'  {target}: already cached, skipping')
            continue
        log('INFO', 'gdelt', f'  Backfilling {target}...')
        try:
            collect_day(target, watchlist, use_master=True)
        except Exception as e:
            log('ERROR', 'gdelt', f'  Backfill failed for {target}: {e}')
        time.sleep(2)
    log('INFO', 'gdelt', 'Backfill complete')


def run(watchlist):
    """Main entry: backfill if needed, then collect today, then compute trends."""
    log('INFO', 'gdelt', f'GDELT collection for {len(watchlist)} tickers')

    # Check if we have 30-day history
    if watchlist:
        sample = next(iter(watchlist))
        threshold = date.today() - timedelta(days=28)
        old_data = query(
            "SELECT COUNT(*) as cnt FROM news_sentiment WHERE ticker=%s AND date<%s",
            (sample, threshold)
        )
        has_history = old_data[0]['cnt'] > 0 if old_data else False

        if not has_history:
            log('INFO', 'gdelt', 'No 30-day history found — running backfill')
            backfill_30_days(watchlist)

    # Collect today's data
    today = date.today()
    log('INFO', 'gdelt', f'Collecting today ({today}) data...')
    try:
        urls = _get_today_urls()
        if urls:
            all_terms_map = {}
            ticker_rows = defaultdict(list)
            for ticker, company_name in watchlist.items():
                terms = set()
                for word in company_name.lower().split():
                    if len(word) > 3:
                        terms.add(word)
                terms.add(ticker.lower())
                all_terms_map[ticker] = list(terms)

            all_terms_flat = set()
            for t in all_terms_map.values():
                all_terms_flat.update(t)

            for url in urls:
                try:
                    resp = requests.get(url, timeout=60)
                    if resp.status_code != 200:
                        continue
                    z = zipfile.ZipFile(io.BytesIO(resp.content))
                    with z.open(z.namelist()[0]) as f:
                        content = f.read().decode('latin-1', errors='ignore')
                    lines = content.split('\n')
                    for ticker, terms in all_terms_map.items():
                        for line in lines:
                            if any(t in line.lower() for t in terms):
                                parts = line.split('\t')
                                if len(parts) > GKG_TONE:
                                    try:
                                        tone = float(parts[GKG_TONE].split(',')[0])
                                        themes = parts[GKG_THEMES] if len(parts) > GKG_THEMES else ''
                                        ticker_rows[ticker].append((tone, themes))
                                    except (ValueError, IndexError):
                                        pass
                except Exception as e:
                    log('WARNING', 'gdelt', f'File error: {e}')

            for ticker in watchlist:
                rows = ticker_rows.get(ticker, [])
                agg = _aggregate_results(rows)
                _save_daily_sentiment(ticker, today, agg)
        else:
            log('WARNING', 'gdelt', 'No today URLs — using empty record')
            for ticker in watchlist:
                if not _already_have_date(ticker, today):
                    _save_daily_sentiment(ticker, today, {
                        'article_count': 0, 'avg_tone': 0.0,
                        'positive_count': 0, 'negative_count': 0, 'top_themes': ''
                    })
    except Exception as e:
        log('ERROR', 'gdelt', f'Today collection error: {e}')

    # Compute trends for each ticker
    for ticker in watchlist:
        try:
            _compute_and_save_trends(ticker)
        except Exception as e:
            log('ERROR', 'gdelt', f'Trend compute failed for {ticker}: {e}')

    log('INFO', 'gdelt', 'GDELT collection complete')


if __name__ == '__main__':
    test_watchlist = {'AAPL': 'Apple', 'NVDA': 'NVIDIA'}
    run(test_watchlist)

    print('\n--- 30-day GDELT trend summary ---')
    for ticker in test_watchlist:
        row = query("""
            SELECT date, article_count, avg_tone, mention_velocity,
                   tone_trajectory, is_accelerating
            FROM news_sentiment WHERE ticker=%s
            ORDER BY date DESC LIMIT 1
        """, (ticker,))
        if row:
            r = row[0]
            acc = 'ACCELERATING ↑' if r['is_accelerating'] else 'stable'
            print(f"{ticker}: avg {r['article_count']} articles, "
                  f"tone {float(r['avg_tone'] or 0):+.2f}, {acc}")
