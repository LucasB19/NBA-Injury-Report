import { createRequire } from "module";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import cheerio from "cheerio";

const require = createRequire(import.meta.url);
const pdfParse = require("pdf-parse");
const execFileAsync = promisify(execFile);

const OFFICIAL_PAGE = "https://official.nba.com/nba-injury-report-2025-26-season/";
const PDF_NAME_PATTERN = /Injury-Report_\d{4}-\d{2}-\d{2}_.+?\.pdf/i;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const fetchWithRetry = async (url, options = {}) => {
  const {
    timeoutMs = 15000,
    retries = 2,
    retryDelayMs = 800,
    responseType = "text",
    headers = {}
  } = options;

  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { signal: controller.signal, headers });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status} on ${url}`);
      }
      if (responseType === "arrayBuffer") {
        return await response.arrayBuffer();
      }
      return await response.text();
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        await sleep(retryDelayMs * (attempt + 1));
      }
    } finally {
      clearTimeout(timeoutId);
    }
  }
  throw lastError;
};

const parsePdfDateTime = (url) => {
  const fileName = url.split("/").pop() || "";
  const match = fileName.match(/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{1,2})(\d{2})?(AM|PM)/i);
  if (!match) {
    return null;
  }
  const [, dateStr, hourStr, minuteStr, meridiem] = match;
  let hour = Number(hourStr);
  const minutes = minuteStr ? Number(minuteStr) : 0;
  if (meridiem.toUpperCase() === "PM" && hour < 12) {
    hour += 12;
  }
  if (meridiem.toUpperCase() === "AM" && hour === 12) {
    hour = 0;
  }
  const isoLike = `${dateStr}T${String(hour).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:00`;
  const timestamp = new Date(isoLike).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
};

const normalizePdfUrl = (href) => {
  try {
    const safeHref = encodeURI(href);
    return new URL(safeHref, OFFICIAL_PAGE).toString();
  } catch {
    return null;
  }
};

const extractPdfLinks = (html) => {
  const $ = cheerio.load(html);
  const links = new Set();
  $("a[href$='.pdf']").each((_, element) => {
    const href = $(element).attr("href");
    if (!href || !PDF_NAME_PATTERN.test(href)) {
      return;
    }
    const url = normalizePdfUrl(href);
    if (url) {
      links.add(url);
    }
  });
  const inlineMatches = html.match(/Injury-Report_[^"'\\s>]+?\\.pdf/gi) || [];
  inlineMatches.forEach((match) => {
    const url = normalizePdfUrl(match);
    if (url) {
      links.add(url);
    }
  });
  return Array.from(links);
};

const selectLatestPdf = (links) => {
  if (!links.length) {
    return null;
  }
  const ranked = links.map((link) => ({
    url: link,
    timestamp: parsePdfDateTime(link) ?? 0
  }));
  ranked.sort((a, b) => b.timestamp - a.timestamp);
  return ranked[0];
};

const parseHeaderLine = (line) => {
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (/NBA INJURY REPORT/i.test(trimmed)) return true;
  if (/Report Updated/i.test(trimmed)) return true;
  if (/Page\s+\d+/i.test(trimmed)) return true;
  if (/GAME DATE/i.test(trimmed) && /MATCHUP/i.test(trimmed)) return true;
  return false;
};

const parseGameAndMatchup = (value) => {
  const match = value.match(/(\d{1,2}:\d{2}\s?[AP]M)\s+([A-Z]{2,3}\s?@\s?[A-Z]{2,3})/i);
  if (!match) {
    return null;
  }
  return {
    time: match[1].replace(/\s+/g, " ").toUpperCase(),
    matchup: match[2].replace(/\s+/g, "")
  };
};

const parseRows = (text) => {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const rows = [];
  let currentRow = null;

  for (const line of lines) {
    if (parseHeaderLine(line)) {
      continue;
    }

    const parts = line.split(/\s{2,}/).map((part) => part.trim()).filter(Boolean);
    if (parts.length >= 5) {
      let gameTime = "";
      let matchup = "";
      let team = "";
      let player = "";
      let status = "";
      let reason = "";

      if (parts.length >= 6) {
        gameTime = parts[0];
        matchup = parts[1];
        team = parts[2];
        player = parts[3];
        status = parts[4];
        reason = parts.slice(5).join(" ");
      } else {
        const parsed = parseGameAndMatchup(parts[0]);
        if (parsed) {
          gameTime = parsed.time;
          matchup = parsed.matchup;
          team = parts[1];
          player = parts[2];
          status = parts[3];
          reason = parts.slice(4).join(" ");
        } else {
          if (currentRow) {
            currentRow.reason = `${currentRow.reason} ${line}`.trim();
          }
          continue;
        }
      }

      const row = {
        gameTime: gameTime || "TBD",
        matchup: matchup || "",
        team,
        player,
        status,
        reason
      };

      rows.push(row);
      currentRow = row;
    } else if (currentRow) {
      currentRow.reason = `${currentRow.reason} ${line}`.trim();
    }
  }

  return rows;
};

const buildStats = (rows) => {
  const byStatus = {};
  const byTeam = {};
  rows.forEach((row) => {
    const status = row.status || "Unknown";
    const team = row.team || "Unknown";
    byStatus[status] = (byStatus[status] || 0) + 1;
    byTeam[team] = (byTeam[team] || 0) + 1;
  });
  return {
    totalRows: rows.length,
    byStatus,
    byTeam
  };
};

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Cache-Control", "s-maxage=300, stale-while-revalidate=900");

  if (process.env.USE_SELENIUM === "1") {
    try {
      const { stdout } = await execFileAsync("python3", ["scripts/injury_report_selenium.py"], {
        timeout: 60000,
        maxBuffer: 10 * 1024 * 1024
      });
      const payload = JSON.parse(stdout);
      if (!payload.ok) {
        res.status(502).json(payload);
        return;
      }
      res.status(200).json(payload);
      return;
    } catch (error) {
      res.status(502).json({
        ok: false,
        error: {
          message: error?.message || "Erreur Selenium inconnue",
          step: "selenium"
        }
      });
      return;
    }
  }

  try {
    const html = await fetchWithRetry(OFFICIAL_PAGE, {
      timeoutMs: 12000,
      retries: 3,
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        Accept: "text/html"
      }
    });
    const pdfLinks = extractPdfLinks(html);

    const latest = selectLatestPdf(pdfLinks);
    if (!latest) {
      res.status(502).json({
        ok: false,
        error: {
          message: "Aucun PDF NBA detecte sur la page officielle.",
          step: "parse_links"
        }
      });
      return;
    }

    const pdfArrayBuffer = await fetchWithRetry(latest.url, {
      timeoutMs: 20000,
      retries: 3,
      responseType: "arrayBuffer",
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        Accept: "application/pdf"
      }
    });

    const pdfBuffer = Buffer.from(pdfArrayBuffer);
    const pdfData = await pdfParse(pdfBuffer);
    const rows = parseRows(pdfData.text || "");

    if (!rows.length) {
      res.status(502).json({
        ok: false,
        error: {
          message: "PDF charge mais aucune ligne exploitable.",
          step: "parse_pdf"
        }
      });
      return;
    }

    res.status(200).json({
      ok: true,
      meta: {
        pdfUrl: latest.url,
        pdfName: latest.url.split("/").pop() || "Injury Report",
        publishedAt: latest.timestamp ? new Date(latest.timestamp).toISOString() : null
      },
      stats: buildStats(rows),
      rows
    });
  } catch (error) {
    res.status(502).json({
      ok: false,
      error: {
        message: error?.message || "Erreur inconnue",
        step: "fetch"
      }
    });
  }
}
