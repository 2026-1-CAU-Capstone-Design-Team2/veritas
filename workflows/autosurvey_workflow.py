from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core.models import DocRecord


ProgressCallback = Callable[..., None]


class AutoSurveyWorkflow:
    def __init__(
        self,
        registry,
        run_store_service,
        *,
        max_docs: int = 15,
        collect_batch_size: int = 5,
        scout_docs: int = 3,
        progress_callback: ProgressCallback | None = None,
    ):
        self.registry = registry
        self.run_store_service = run_store_service
        self.max_docs = max(1, int(max_docs))
        self.collect_batch_size = max(1, int(collect_batch_size))
        self.scout_docs = max(1, min(int(scout_docs), self.max_docs))
        self._progress_callback = progress_callback

    def _emit_progress(
        self,
        stage: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(stage, message, detail=detail or {})
        except TypeError:
            try:
                self._progress_callback(stage, message)
            except Exception:
                pass
        except Exception:
            pass

    def run_term_grounding(
        self,
        user_request: str,
        *,
        force: bool = False,
        max_terms: int = 8,
    ) -> dict[str, Any]:
        request_text = (user_request or "").strip()
        if not request_text:
            raise RuntimeError("`user_request` must be non-empty for term grounding")

        if self.run_store_service.grounding_exists() and not force:
            try:
                grounded = self.run_store_service.load_grounding()
                if grounded.get("request_text", "").strip() == request_text:
                    return grounded
            except Exception:
                pass

        self._emit_progress("term_grounding", "주제어 추출 중...")
        result = self.registry.get("term_grounding").run(
            user_request=request_text,
            max_terms=max_terms,
        )
        if not result.success:
            raise RuntimeError(result.error)

        grounded = dict(result.data or {})
        grounded["request_text"] = request_text
        self.run_store_service.save_grounding(grounded)
        return grounded

    def run_plan(
        self,
        user_request: str,
        *,
        force_plan: bool = False,
        mode: str = "initial",
        grounding: dict[str, Any] | None = None,
        prior_plan: dict[str, Any] | None = None,
        gap_directions: list[str] | None = None,
        save_request: bool = True,
    ) -> dict[str, Any]:
        if save_request:
            self.run_store_service.save_request(user_request)

        previous_plan_snapshot: dict[str, Any] = {}
        if prior_plan is not None:
            previous_plan_snapshot = prior_plan
        elif self.run_store_service.plan_exists():
            try:
                previous_plan_snapshot = self.run_store_service.load_plan()
            except Exception:
                previous_plan_snapshot = {}

        query_state = self.run_store_service.load_query_state()
        used_queries = query_state.get("used_queries", [])

        plan_label = "검색 계획 수립 중..." if mode == "initial" else "검색 계획 재구성 중..."
        self._emit_progress("query_plan", plan_label, detail={"mode": mode})
        result = self.registry.get("query_plan").run(
            user_request=user_request,
            force=force_plan,
            mode=mode,
            grounding=grounding,
            prior_plan=prior_plan,
            gap_directions=gap_directions or [],
            used_queries=used_queries,
            save=True,
        )
        if not result.success:
            raise RuntimeError(result.error)

        plan = dict(result.data or {})
        reference_sites = self._reference_sites_from_inputs(
            user_request=user_request,
            grounding=grounding,
        )
        if reference_sites:
            plan = self._apply_reference_sites_to_plan(
                plan,
                reference_sites=reference_sites,
            )
            self.run_store_service.save_plan(plan)

        self.run_store_service.append_plan_history(
            reason=mode,
            plan=plan,
            previous_plan=previous_plan_snapshot,
            gap_directions=gap_directions or [],
        )

        changed = previous_plan_snapshot != plan
        if mode == "replan":
            if changed:
                print("[plan] replan executed and plan.json overwritten")
            else:
                print("[plan] replan executed but produced an identical plan")

        return plan

    def run_collect(
        self,
        plan: dict[str, Any],
        *,
        max_new_docs: int | None = None,
        queries: list[str] | None = None,
        phase_label: str = "main",
    ) -> dict[str, Any]:
        kept_count = self._kept_record_count()
        remaining_capacity = max(0, self.max_docs - kept_count)
        target_new_docs = remaining_capacity
        if max_new_docs is not None:
            target_new_docs = min(target_new_docs, max(0, int(max_new_docs)))

        if target_new_docs == 0:
            print(
                "[collect][skip:capacity] "
                f"phase={phase_label} kept={kept_count} max_docs={self.max_docs}"
            )
            return {
                "record_count": len(self.run_store_service.load_records()),
                "kept_record_count": kept_count,
                "new_doc_ids": [],
                "new_doc_count": 0,
                "consumed_queries": [],
                "skipped_used_queries": [],
                "target_new_docs": 0,
            }

        query_state = self.run_store_service.load_query_state()
        used_queries = list(query_state.get("used_queries", []))
        used_query_keys = {self._normalize_query(query) for query in used_queries}

        search_queries = [
            str(query).strip()
            for query in (queries if queries is not None else plan.get("search_queries", []))
            if str(query).strip()
        ]
        print(
            "[collect][start] "
            f"phase={phase_label} target_new_docs={target_new_docs} "
            f"queries={len(search_queries)} kept={kept_count}/{self.max_docs}"
        )

        new_doc_ids: list[str] = []
        duplicate_doc_ids: list[str] = []
        fetch_error_doc_ids: list[str] = []
        consumed_queries: list[str] = []
        skipped_used_queries: list[str] = []

        for query in search_queries:
            if len(new_doc_ids) >= target_new_docs:
                break

            normalized_query = self._normalize_query(query)
            if normalized_query in used_query_keys:
                skipped_used_queries.append(query)
                continue

            consumed_queries.append(query)
            used_query_keys.add(normalized_query)
            used_queries.append(query)

            self._emit_progress(
                "web_search",
                f"검색 중: {query}",
                detail={"query": query, "phase": phase_label},
            )
            search_result = self.registry.get("web_search").run(
                query=query,
                num_results=max(5, min(10, target_new_docs * 3)),
            )
            if not search_result.success:
                print(
                    "[collect][search-failed] "
                    f"phase={phase_label} query={query!r} "
                    f"error={self._compact_error(search_result.error)}"
                )
                continue

            results = search_result.data.get("results", [])
            print(
                "[collect][search] "
                f"phase={phase_label} query={query!r} results={len(results)}"
            )
            for item in results:
                if len(new_doc_ids) >= target_new_docs:
                    break

                if self._kept_record_count() >= self.max_docs:
                    break

                url = str(item.get("link", "")).strip()
                title = str(item.get("title", "")).strip()

                if not url:
                    continue

                records_now = self.run_store_service.load_records()
                if self._already_seen_url(records_now, url):
                    continue

                fetch_result = self._fetch_one(title_hint=title, url=url, query=query)
                status = fetch_result.get("status")
                doc_id = fetch_result.get("doc_id")

                if status == "fetched" and doc_id:
                    new_doc_ids.append(doc_id)
                    print(f"[collect][fetched] phase={phase_label} doc_id={doc_id}")
                elif status == "duplicate" and doc_id:
                    duplicate_doc_ids.append(doc_id)
                    print(f"[collect][duplicate] phase={phase_label} doc_id={doc_id}")
                elif status == "fetch_error" and doc_id:
                    fetch_error_doc_ids.append(doc_id)
                    print(f"[collect][fetch-error] phase={phase_label} doc_id={doc_id}")

        query_state["used_queries"] = used_queries
        query_state["cycles_executed"] = int(query_state.get("cycles_executed", 0) or 0) + 1
        query_state["last_phase"] = phase_label
        query_state["last_consumed_queries"] = consumed_queries
        query_state["last_new_doc_ids"] = new_doc_ids
        self.run_store_service.save_query_state(query_state)

        return {
            "record_count": len(self.run_store_service.load_records()),
            "kept_record_count": self._kept_record_count(),
            "new_doc_ids": new_doc_ids,
            "new_doc_count": len(new_doc_ids),
            "duplicate_doc_ids": duplicate_doc_ids,
            "fetch_error_doc_ids": fetch_error_doc_ids,
            "consumed_queries": consumed_queries,
            "skipped_used_queries": skipped_used_queries,
            "target_new_docs": target_new_docs,
        }

    def run_summarize(
        self,
        *,
        overwrite: bool = False,
        doc_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if doc_ids is not None and not doc_ids:
            print("[summarize][skip:no-new-docs] empty doc id list")
            return {
                "summarized_doc_ids": [],
                "skipped_existing_doc_ids": [],
                "skipped_invalid_doc_ids": [],
                "skipped_duplicate_doc_ids": [],
                "skipped_not_in_cycle_doc_ids": [],
                "failed_doc_ids": [],
                "failed_documents": [],
                "batch_result": {"batch_files": [], "count": 0},
            }

        doc_count = len(doc_ids) if doc_ids is not None else 0
        message = (
            f"문서 요약 중: {doc_count}건" if doc_count > 0 else "문서 요약 중..."
        )
        self._emit_progress(
            "document_summarize",
            message,
            detail={"doc_count": doc_count, "doc_ids": doc_ids or []},
        )
        result = self.registry.get("document_summarize").run(
            overwrite=overwrite,
            doc_ids=doc_ids,
            rebuild_batches=True,
        )
        if not result.success:
            raise RuntimeError(result.error)
        data = result.data or {}
        for summarized_id in data.get("summarized_doc_ids", []) or []:
            doc_id_str = str(summarized_id)
            try:
                summary_path = self.run_store_service.paths.summary_path_for(
                    int(doc_id_str)
                )
                summary_path_str = str(summary_path.resolve())
            except Exception:
                summary_path_str = ""
            self._emit_progress(
                "doc_summarized",
                f"요약 완료: doc_{doc_id_str}",
                detail={
                    "doc_id": doc_id_str,
                    "summary_path": summary_path_str,
                },
            )
        for failed in data.get("failed_documents", []) or []:
            if not isinstance(failed, dict):
                continue
            doc_id_str = str(failed.get("docId") or "").strip()
            if not doc_id_str:
                continue
            reason = str(failed.get("reason") or "요약 실패").strip()
            self._emit_progress(
                "doc_failed",
                f"요약 실패: doc_{doc_id_str}",
                detail={
                    "doc_id": doc_id_str,
                    "title": str(failed.get("title") or doc_id_str),
                    "reason": reason,
                },
            )
        return data

    def run_final(self, *, user_request: str | None = None) -> dict[str, Any]:
        self._emit_progress("final_report", "최종 보고서 작성 중...")
        result = self.registry.get("final_report").run(user_request=user_request)
        if not result.success:
            raise RuntimeError(result.error)
        return result.data

    def run_all(
        self,
        user_request: str,
        *,
        force_plan: bool = False,
        overwrite_summaries: bool = False,
        grounding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.run_store_service.save_request(user_request)
        self.run_store_service.reset_query_state()

        if grounding is None:
            grounding = self.run_term_grounding(user_request=user_request, force=True)
        else:
            grounding = dict(grounding)
            grounding["request_text"] = user_request
            self.run_store_service.save_grounding(grounding)
        reference_sites = self._extract_reference_sites(user_request)
        if reference_sites:
            grounding["reference_sites"] = reference_sites
            self.run_store_service.save_grounding(grounding)

        reference_collect_result = self._collect_reference_sites(reference_sites)
        if reference_collect_result.get("new_doc_count", 0) > 0:
            reference_summarize_result = self.run_summarize(
                overwrite=overwrite_summaries,
                doc_ids=reference_collect_result.get("new_doc_ids", []),
            )
        else:
            reference_summarize_result = {
                "summarized_doc_ids": [],
                "skipped_reason": "no_reference_docs",
            }

        initial_plan = self.run_plan(
            user_request=user_request,
            force_plan=force_plan,
            mode="initial",
            grounding=grounding,
            save_request=False,
        )

        # term_grounding extracts only terms; initial_plan owns query generation.
        scout_queries = initial_plan.get("search_queries", [])
        scout_collect_result = self.run_collect(
            plan=initial_plan,
            max_new_docs=self.scout_docs,
            queries=scout_queries,
            phase_label="scout",
        )

        if scout_collect_result.get("new_doc_count", 0) > 0:
            scout_summarize_result = self.run_summarize(
                overwrite=overwrite_summaries,
                doc_ids=scout_collect_result.get("new_doc_ids", []),
            )
        else:
            print("[summarize][skip:no-new-docs] scout cycle produced no new documents")
            scout_summarize_result = {
                "summarized_doc_ids": [],
                "skipped_reason": "no_new_docs",
            }

        scout_gap_directions = (
            self._gap_directions_from_summarize_result(reference_summarize_result)
            + self._gap_directions_from_summarize_result(scout_summarize_result)
        )
        if scout_gap_directions:
            active_plan = self.run_plan(
                user_request=user_request,
                force_plan=True,
                mode="replan",
                grounding=grounding,
                prior_plan=initial_plan,
                gap_directions=scout_gap_directions,
                save_request=False,
            )
        else:
            print("[plan] scout replan skipped: no concrete gap directions")
            active_plan = initial_plan

        iterations: list[dict[str, Any]] = []
        loop_index = 0
        empty_collect_replans = 0
        max_empty_collect_replans = 2

        while self._kept_record_count() < self.max_docs:
            loop_index += 1
            collect_result = self.run_collect(
                plan=active_plan,
                max_new_docs=self.collect_batch_size,
                phase_label=f"main-{loop_index}",
            )

            if collect_result.get("new_doc_count", 0) == 0:
                print("[summarize][skip:no-new-docs] main cycle produced no new documents")
                if empty_collect_replans < max_empty_collect_replans:
                    next_plan = self.run_plan(
                        user_request=user_request,
                        force_plan=True,
                        mode="replan",
                        grounding=grounding,
                        prior_plan=active_plan,
                        gap_directions=[],
                        save_request=False,
                    )
                    empty_collect_replans += 1
                    if self._remaining_search_queries(next_plan):
                        print(
                            "[plan] recovered additional queries after empty collect cycle"
                        )
                        iterations.append(
                            {
                                "iteration": loop_index,
                                "collect_result": collect_result,
                                "summarize_result": {"skipped_reason": "no_new_docs"},
                                "gap_directions": [],
                                "replan_changed": active_plan != next_plan,
                                "replan_skipped_reason": None,
                            }
                        )
                        active_plan = next_plan
                        continue

                iterations.append(
                    {
                        "iteration": loop_index,
                        "collect_result": collect_result,
                        "summarize_result": {"skipped_reason": "no_new_docs"},
                        "gap_directions": [],
                        "replan_changed": False,
                        "replan_skipped_reason": "no_new_docs",
                    }
                )
                break

            empty_collect_replans = 0
            summarize_result = self.run_summarize(
                overwrite=overwrite_summaries,
                doc_ids=collect_result.get("new_doc_ids", []),
            )

            gap_directions = self._gap_directions_from_summarize_result(summarize_result)

            if self._kept_record_count() >= self.max_docs:
                print("[plan] replan skipped: max_docs reached after summarize")
                iterations.append(
                    {
                        "iteration": loop_index,
                        "collect_result": collect_result,
                        "summarize_result": summarize_result,
                        "gap_directions": gap_directions,
                        "replan_changed": False,
                        "replan_skipped_reason": "max_docs_reached",
                    }
                )
                break

            if not gap_directions:
                print("[plan] replan skipped: no concrete gap directions")
                if not self._remaining_search_queries(active_plan):
                    next_plan = self.run_plan(
                        user_request=user_request,
                        force_plan=True,
                        mode="replan",
                        grounding=grounding,
                        prior_plan=active_plan,
                        gap_directions=[],
                        save_request=False,
                    )
                    if self._remaining_search_queries(next_plan):
                        print("[plan] recovered additional queries after query exhaustion")
                        iterations.append(
                            {
                                "iteration": loop_index,
                                "collect_result": collect_result,
                                "summarize_result": summarize_result,
                                "gap_directions": gap_directions,
                                "replan_changed": active_plan != next_plan,
                                "replan_skipped_reason": None,
                            }
                        )
                        active_plan = next_plan
                        continue

                iterations.append(
                    {
                        "iteration": loop_index,
                        "collect_result": collect_result,
                        "summarize_result": summarize_result,
                        "gap_directions": gap_directions,
                        "replan_changed": False,
                        "replan_skipped_reason": "no_gap_directions",
                    }
                )
                continue

            next_plan = self.run_plan(
                user_request=user_request,
                force_plan=True,
                mode="replan",
                grounding=grounding,
                prior_plan=active_plan,
                gap_directions=gap_directions,
                save_request=False,
            )

            replan_changed = active_plan != next_plan
            iterations.append(
                {
                    "iteration": loop_index,
                    "collect_result": collect_result,
                    "summarize_result": summarize_result,
                    "gap_directions": gap_directions,
                    "replan_changed": replan_changed,
                    "replan_skipped_reason": None,
                }
            )

            active_plan = next_plan
            if not active_plan.get("search_queries"):
                print("[workflow] stopping loop: no remaining search queries after replan")
                break

        final_result = self.run_final(user_request=user_request)

        summarize_results = [reference_summarize_result, scout_summarize_result]
        summarize_results.extend(
            iteration.get("summarize_result")
            for iteration in iterations
            if isinstance(iteration, dict)
        )
        failed_documents = self._aggregate_failed_documents(summarize_results)

        return {
            "grounding": grounding,
            "reference_collect_result": reference_collect_result,
            "reference_summarize_result": reference_summarize_result,
            "initial_plan": initial_plan,
            "scout_collect_result": scout_collect_result,
            "scout_summarize_result": scout_summarize_result,
            "active_plan": active_plan,
            "iterations": iterations,
            "final_result": final_result,
            "failed_documents": failed_documents,
        }

    def _aggregate_failed_documents(
        self,
        summarize_results: list[Any],
    ) -> list[dict[str, str]]:
        """Collect per-document summarization failures across every cycle.

        Each ``run_summarize`` call returns its own ``failed_documents`` list;
        this merges them (deduped by docId) so the run as a whole can report
        exactly which documents could not be summarized and why.
        """
        aggregated: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in summarize_results:
            if not isinstance(result, dict):
                continue
            for failed in result.get("failed_documents", []) or []:
                if not isinstance(failed, dict):
                    continue
                doc_id = str(failed.get("docId") or "").strip()
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                aggregated.append(
                    {
                        "docId": doc_id,
                        "title": str(failed.get("title") or doc_id),
                        "reason": str(failed.get("reason") or "요약 실패"),
                    }
                )
        return aggregated

    def _already_seen_url(self, records: list[DocRecord], url: str) -> bool:
        canonical_url = self._canonicalize_url(url)
        return any(
            self._canonicalize_url(r.final_url) == canonical_url
            or self._canonicalize_url(r.url) == canonical_url
            for r in records
        )

    def _fetch_one(self, title_hint: str, url: str, query: str) -> dict[str, Any]:
        records = self.run_store_service.load_records()
        doc_id = self.run_store_service.next_doc_id(len(records))

        domain = urlparse(url).netloc or url
        self._emit_progress(
            "fetch_webpage",
            f"문서 수집 중: {domain}",
            detail={"url": url, "title": title_hint},
        )
        fetch_result = self.registry.get("fetch_webpage").run(
            url=url,
            timeout_sec=15,
            max_chars=25000,
        )
        if not fetch_result.success:
            self.run_store_service.write_fetch_error_note(
                doc_id=doc_id,
                url=url,
                error=fetch_result.error or "unknown error",
            )
            return {"status": "fetch_error", "doc_id": doc_id}

        fetched = fetch_result.data
        stored_url = self._canonicalize_url(getattr(fetched, "url", "") or url) or url
        stored_final_url = (
            self._canonicalize_url(getattr(fetched, "final_url", "") or stored_url)
            or stored_url
        )

        is_dup, dup_score, duplicate_of = self.run_store_service.find_duplicate(
            fetched.text,
            url=stored_url,
            final_url=stored_final_url,
            title=fetched.title or title_hint or "Untitled",
        )
        if is_dup and duplicate_of is not None:
            self.run_store_service.write_duplicate_record(
                doc_id=doc_id,
                title=fetched.title or title_hint or "Untitled",
                url=stored_url,
                final_url=stored_final_url,
                domain=fetched.domain,
                search_query=query,
                duplicate_of=duplicate_of,
                duplicate_score=dup_score,
            )
            return {
                "status": "duplicate",
                "doc_id": doc_id,
                "duplicate_of": duplicate_of,
                "duplicate_score": dup_score,
            }

        self.run_store_service.write_fetched_record(
            doc_id=doc_id,
            title=fetched.title or title_hint or "Untitled",
            url=stored_url,
            final_url=stored_final_url,
            domain=fetched.domain,
            search_query=query,
            html=fetched.html,
            text=fetched.text,
            content_type=getattr(fetched, "content_type", ""),
        )
        stored_title = fetched.title or title_hint or "Untitled"
        self._emit_progress(
            "doc_fetched",
            f"문서 수집 완료: {stored_title}",
            detail={
                "doc_id": doc_id,
                "title": stored_title,
                "url": stored_url,
                "final_url": stored_final_url,
                "domain": fetched.domain,
            },
        )
        return {"status": "fetched", "doc_id": doc_id}

    def _kept_record_count(self) -> int:
        return len(self.run_store_service.list_non_duplicate_records())

    def _extract_reference_sites(self, user_request: str) -> list[dict[str, str]]:
        sites: list[dict[str, str]] = []
        seen: set[str] = set()

        for match in re.finditer(r"\bsite:([^\s,;)\]}]+)", str(user_request or ""), flags=re.IGNORECASE):
            raw_target = match.group(1).strip().strip("\"'`<>")
            raw_target = raw_target.rstrip(".,")
            normalized = self._normalize_reference_site(raw_target)
            if not normalized:
                continue

            key = normalized["search_operator"].lower()
            if key in seen:
                continue
            seen.add(key)
            sites.append(normalized)

        return sites

    def _normalize_reference_site(self, raw_target: str) -> dict[str, str] | None:
        target = str(raw_target or "").strip()
        if not target:
            return None

        parsed = urlparse(target if "://" in target else f"https://{target}")
        domain = (parsed.netloc or parsed.path.split("/", 1)[0]).lower().strip()
        if not domain:
            return None

        domain = domain.split("@")[-1].split(":")[0].strip()
        if not domain or "." not in domain:
            return None

        path = parsed.path if parsed.netloc else ""
        if path and path == "/":
            path = ""

        reference_url = f"{parsed.scheme or 'https'}://{domain}{path}"
        search_scope = f"{domain}{path}".rstrip("/")
        return {
            "raw": target,
            "domain": domain,
            "url": reference_url,
            "search_operator": f"site:{search_scope}",
        }

    def _reference_sites_from_inputs(
        self,
        *,
        user_request: str,
        grounding: dict[str, Any] | None,
    ) -> list[dict[str, str]]:
        sites: list[dict[str, str]] = []
        if isinstance(grounding, dict):
            for item in grounding.get("reference_sites", []):
                if isinstance(item, dict):
                    normalized = self._normalize_reference_site(
                        item.get("url") or item.get("raw") or item.get("domain") or ""
                    )
                    if normalized:
                        sites.append(normalized)

        sites.extend(self._extract_reference_sites(user_request))

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for site in sites:
            key = site["search_operator"].lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(site)
        return deduped

    def _collect_reference_sites(self, reference_sites: list[dict[str, str]]) -> dict[str, Any]:
        if not reference_sites:
            return {
                "reference_sites": [],
                "new_doc_ids": [],
                "duplicate_doc_ids": [],
                "fetch_error_doc_ids": [],
            }

        new_doc_ids: list[str] = []
        duplicate_doc_ids: list[str] = []
        fetch_error_doc_ids: list[str] = []

        for site in reference_sites:
            if self._kept_record_count() >= self.max_docs:
                break

            result = self._fetch_one(
                title_hint=f"Reference site: {site['domain']}",
                url=site["url"],
                query=site["search_operator"],
            )
            status = result.get("status")
            doc_id = result.get("doc_id")
            if status == "fetched" and doc_id:
                new_doc_ids.append(doc_id)
            elif status == "duplicate" and doc_id:
                duplicate_doc_ids.append(doc_id)
            elif status == "fetch_error" and doc_id:
                fetch_error_doc_ids.append(doc_id)

        return {
            "reference_sites": reference_sites,
            "new_doc_ids": new_doc_ids,
            "new_doc_count": len(new_doc_ids),
            "duplicate_doc_ids": duplicate_doc_ids,
            "fetch_error_doc_ids": fetch_error_doc_ids,
        }

    def _apply_reference_sites_to_plan(
        self,
        plan: dict[str, Any],
        *,
        reference_sites: list[dict[str, str]],
    ) -> dict[str, Any]:
        plan = dict(plan or {})
        original_queries = [
            str(query).strip()
            for query in plan.get("search_queries", [])
            if str(query).strip()
        ]

        site_queries: list[str] = []
        for site in reference_sites:
            operator = site["search_operator"]
            for query in original_queries or [str(plan.get("topic") or "").strip()]:
                clean_query = self._remove_site_operators(query)
                if clean_query:
                    site_queries.append(f"{operator} {clean_query}")
                else:
                    site_queries.append(operator)

        plan["search_queries"] = self._dedupe_queries(site_queries + original_queries, max_items=20)
        plan["reference_sites"] = reference_sites

        must_cover = [
            str(item).strip()
            for item in plan.get("must_cover", [])
            if str(item).strip()
        ]
        must_cover.extend(
            f"Include evidence from {site['search_operator']}"
            for site in reference_sites
        )
        plan["must_cover"] = self._dedupe_queries(must_cover, max_items=20)
        return plan

    def _remove_site_operators(self, query: str) -> str:
        return re.sub(r"\bsite:[^\s,;)\]}]+", "", str(query or ""), flags=re.IGNORECASE).strip()

    def _dedupe_queries(self, queries: list[str], *, max_items: int) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for query in queries:
            text = str(query).strip()
            key = self._normalize_query(text)
            if not text or key in seen:
                continue
            seen.add(key)
            deduped.append(text)
            if len(deduped) >= max_items:
                break
        return deduped

    def _remaining_search_queries(self, plan: dict[str, Any]) -> list[str]:
        query_state = self.run_store_service.load_query_state()
        used_query_keys = {
            self._normalize_query(query)
            for query in query_state.get("used_queries", [])
        }
        remaining: list[str] = []
        for query in plan.get("search_queries", []):
            text = str(query).strip()
            key = self._normalize_query(text)
            if text and key not in used_query_keys:
                remaining.append(text)
        return remaining

    def _gap_directions_from_summarize_result(
        self,
        summarize_result: dict[str, Any],
        *,
        max_items: int = 12,
    ) -> list[str]:
        batch_result = summarize_result.get("batch_result") if isinstance(summarize_result, dict) else {}
        batch_files = batch_result.get("batch_files", []) if isinstance(batch_result, dict) else []
        if not isinstance(batch_files, list) or not batch_files:
            return []

        markdowns: list[str] = []
        for file_path in batch_files:
            path = Path(str(file_path).strip())
            if not path.exists() or path.stat().st_size == 0:
                continue
            try:
                markdowns.append(path.read_text(encoding="utf-8"))
            except Exception:
                continue

        return self._extract_gap_directions_from_markdowns(markdowns, max_items=max_items)

    def _load_gap_directions(self, *, max_items: int = 12) -> list[str]:
        batch_summaries = self.run_store_service.load_all_batch_summaries()
        if not batch_summaries:
            return []
        return self._extract_gap_directions_from_markdowns([batch_summaries[-1]], max_items=max_items)

    def _extract_gap_directions_from_markdowns(
        self,
        markdowns: list[str],
        *,
        max_items: int,
    ) -> list[str]:
        if not markdowns:
            return []

        directions: list[str] = []
        for markdown in reversed(markdowns):
            directions.extend(self._extract_gap_direction_lines(markdown))

        deduped: list[str] = []
        seen: set[str] = set()
        for item in directions:
            key = self._normalize_query(item)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= max_items:
                break

        return deduped

    def _extract_gap_direction_lines(self, markdown: str) -> list[str]:
        lines = markdown.splitlines()
        in_gap_section = False
        active_gap_bucket = ""
        saw_core_heading = False
        core_extracted: list[str] = []
        legacy_extracted: list[str] = []

        for raw_line in lines:
            line = raw_line.strip()
            if line.lower().startswith("## "):
                heading = line[3:].strip().lower()
                in_gap_section = heading.startswith("gaps / next search directions")
                active_gap_bucket = ""
                continue

            if not in_gap_section:
                continue

            if not line:
                continue

            if line.lower().startswith("### "):
                sub_heading = line[4:].strip().lower()
                if sub_heading.startswith("core gap"):
                    active_gap_bucket = "core"
                    saw_core_heading = True
                elif sub_heading.startswith("supporting gap"):
                    active_gap_bucket = "supporting"
                elif sub_heading.startswith("off-topic") or sub_heading.startswith("off topic"):
                    active_gap_bucket = "offtopic"
                elif sub_heading.startswith("incidental gap"):
                    active_gap_bucket = "offtopic"
                else:
                    active_gap_bucket = ""
                continue

            if line.startswith("|"):
                continue

            if re.fullmatch(r"[-_*=]{3,}", line):
                continue

            bullet_match = re.match(r"^[\-*]\s+(.*)$", line)
            if bullet_match:
                candidate = bullet_match.group(1).strip()
            else:
                candidate = line.strip()

            candidate = re.sub(r"^\d+\.\s+", "", candidate).strip()
            candidate = re.sub(r"^\*\*note:\*\*\s*", "", candidate, flags=re.IGNORECASE)
            candidate = candidate.strip(" -*")

            if not candidate:
                continue

            if candidate.startswith("|") or re.fullmatch(r"[-_*=]{3,}", candidate):
                continue

            if candidate:
                legacy_extracted.append(candidate)

                if active_gap_bucket == "core":
                    # Remove optional relevance suffix from batch writer format.
                    candidate = re.sub(
                        r"\s*-\s*relevance\s*:\s*.*$",
                        "",
                        candidate,
                        flags=re.IGNORECASE,
                    ).strip()
                    if candidate and candidate.lower() != "none":
                        core_extracted.append(candidate)

        if saw_core_heading:
            return core_extracted

        return legacy_extracted

    def _normalize_query(self, query: str) -> str:
        return " ".join(str(query).strip().lower().split())

    def _canonicalize_url(self, url: str) -> str:
        return self.run_store_service.canonicalize_url(url)

    def _compact_error(self, error: Any, *, max_chars: int = 300) -> str:
        text = re.sub(r"\s+", " ", str(error or "")).strip()
        if len(text) > max_chars:
            return text[: max_chars - 3] + "..."
        return text
