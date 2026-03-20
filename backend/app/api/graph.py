"""
图谱相关API路由
采用项目上下文机制，服务端持久化状态
"""

import os
import time
import uuid
import traceback
import threading
from flask import request, jsonify

from . import graph_bp
from ..config import Config
from ..services.ontology_generator import OntologyGenerator
from ..services.graph_builder import GraphBuilderService
from ..services.text_processor import TextProcessor
from ..utils.file_parser import FileParser
from ..utils.logger import get_logger
from ..models.task import TaskManager, TaskStatus
from ..models.project import ProjectManager, ProjectStatus

# 获取日志器
logger = get_logger('mirofish.api')

# In-memory store for two-step seed research flow (web_only only)
pending_research: dict = {}  # research_id -> {query, simulation_requirement, raw_results, sub_queries, created_at}


def _cleanup_pending_research():
    """Remove entries older than 30 minutes."""
    cutoff = time.time() - 1800
    expired = [rid for rid, e in list(pending_research.items()) if e["created_at"] < cutoff]
    for rid in expired:
        del pending_research[rid]


def allowed_file(filename: str) -> bool:
    """检查文件扩展名是否允许"""
    if not filename or '.' not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in Config.ALLOWED_EXTENSIONS


# ============== 项目管理接口 ==============

@graph_bp.route('/project/<project_id>', methods=['GET'])
def get_project(project_id: str):
    """
    获取项目详情
    """
    project = ProjectManager.get_project(project_id)
    
    if not project:
        return jsonify({
            "success": False,
            "error": f"Project not found: {project_id}"
        }), 404

    return jsonify({
        "success": True,
        "data": project.to_dict()
    })


@graph_bp.route('/project/list', methods=['GET'])
def list_projects():
    """
    列出所有项目
    """
    limit = request.args.get('limit', 50, type=int)
    projects = ProjectManager.list_projects(limit=limit)
    
    return jsonify({
        "success": True,
        "data": [p.to_dict() for p in projects],
        "count": len(projects)
    })


@graph_bp.route('/project/<project_id>', methods=['DELETE'])
def delete_project(project_id: str):
    """
    删除项目
    """
    success = ProjectManager.delete_project(project_id)
    
    if not success:
        return jsonify({
            "success": False,
            "error": f"Project not found or deletion failed: {project_id}"
        }), 404

    return jsonify({
        "success": True,
        "message": f"Project deleted: {project_id}"
    })


@graph_bp.route('/project/<project_id>/reset', methods=['POST'])
def reset_project(project_id: str):
    """
    重置项目状态（用于重新构建图谱）
    """
    project = ProjectManager.get_project(project_id)
    
    if not project:
        return jsonify({
            "success": False,
            "error": f"Project not found: {project_id}"
        }), 404

    # 重置到本体已生成状态
    if project.ontology:
        project.status = ProjectStatus.ONTOLOGY_GENERATED
    else:
        project.status = ProjectStatus.CREATED
    
    project.graph_id = None
    project.graph_build_task_id = None
    project.error = None
    ProjectManager.save_project(project)
    
    return jsonify({
        "success": True,
        "message": f"Project reset: {project_id}",
        "data": project.to_dict()
    })


# ============== 接口1：上传文件并生成本体 ==============

@graph_bp.route('/ontology/generate', methods=['POST'])
def generate_ontology():
    """
    接口1：上传文件，分析生成本体定义
    
    请求方式：multipart/form-data
    
    参数：
        files: 上传的文件（PDF/MD/TXT），可多个
        simulation_requirement: 模拟需求描述（必填）
        project_name: 项目名称（可选）
        additional_context: 额外说明（可选）
        
    返回：
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "ontology": {
                    "entity_types": [...],
                    "edge_types": [...],
                    "analysis_summary": "..."
                },
                "files": [...],
                "total_text_length": 12345
            }
        }
    """
    try:
        logger.info("=== Starting ontology generation ===")
        
        # 获取参数
        simulation_requirement = request.form.get('simulation_requirement', '')
        project_name = request.form.get('project_name', 'Unnamed Project')
        additional_context = request.form.get('additional_context', '')
        
        logger.debug(f"Project name: {project_name}")
        logger.debug(f"Simulation requirement: {simulation_requirement[:100]}...")
        
        if not simulation_requirement:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_requirement"
            }), 400
        
        # 获取上传的文件
        uploaded_files = request.files.getlist('files')
        if not uploaded_files or all(not f.filename for f in uploaded_files):
            return jsonify({
                "success": False,
                "error": "Please upload at least one document"
            }), 400
        
        # 创建项目
        project = ProjectManager.create_project(name=project_name)
        project.simulation_requirement = simulation_requirement
        logger.info(f"Project created: {project.project_id}")
        
        # 保存文件并提取文本
        document_texts = []
        all_text = ""
        
        for file in uploaded_files:
            if file and file.filename and allowed_file(file.filename):
                # 保存文件到项目目录
                file_info = ProjectManager.save_file_to_project(
                    project.project_id, 
                    file, 
                    file.filename
                )
                project.files.append({
                    "filename": file_info["original_filename"],
                    "size": file_info["size"]
                })
                
                # 提取文本
                text = FileParser.extract_text(file_info["path"])
                text = TextProcessor.preprocess_text(text)
                document_texts.append(text)
                all_text += f"\n\n=== {file_info['original_filename']} ===\n{text}"
        
        if not document_texts:
            ProjectManager.delete_project(project.project_id)
            return jsonify({
                "success": False,
                "error": "No documents processed successfully, check file format"
            }), 400
        
        # 保存提取的文本
        project.total_text_length = len(all_text)
        ProjectManager.save_extracted_text(project.project_id, all_text)
        logger.info(f"Text extraction complete, total {len(all_text)} characters")
        
        # 生成本体
        logger.info("Calling LLM to generate ontology...")
        generator = OntologyGenerator()
        ontology = generator.generate(
            document_texts=document_texts,
            simulation_requirement=simulation_requirement,
            additional_context=additional_context if additional_context else None
        )
        
        # 保存本体到项目
        entity_count = len(ontology.get("entity_types", []))
        edge_count = len(ontology.get("edge_types", []))
        logger.info(f"Ontology generation complete: {entity_count} entity types, {edge_count} edge types")
        
        project.ontology = {
            "entity_types": ontology.get("entity_types", []),
            "edge_types": ontology.get("edge_types", [])
        }
        project.analysis_summary = ontology.get("analysis_summary", "")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        ProjectManager.save_project(project)
        logger.info(f"=== Ontology generation complete === Project ID: {project.project_id}")
        
        return jsonify({
            "success": True,
            "data": {
                "project_id": project.project_id,
                "project_name": project.name,
                "ontology": project.ontology,
                "analysis_summary": project.analysis_summary,
                "files": project.files,
                "total_text_length": project.total_text_length
            }
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 接口2：构建图谱 ==============

@graph_bp.route('/build', methods=['POST'])
def build_graph():
    """
    接口2：根据project_id构建图谱
    
    请求（JSON）：
        {
            "project_id": "proj_xxxx",  // 必填，来自接口1
            "graph_name": "图谱名称",    // 可选
            "chunk_size": 1500,         // 可选，默认1500
            "chunk_overlap": 200        // 可选，默认200
        }
        
    返回：
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "task_id": "task_xxxx",
                "message": "图谱构建任务已启动"
            }
        }
    """
    try:
        logger.info("=== Starting graph build ===")
        
        # 检查配置
        errors = []
        if not Config.ZEP_API_KEY:
            errors.append("ZEP_API_KEY not configured")
        if errors:
            logger.error(f"Config error: {errors}")
            return jsonify({
                "success": False,
                "error": "Config error: " + "; ".join(errors)
            }), 500
        
        # 解析请求
        data = request.get_json() or {}
        project_id = data.get('project_id')
        logger.debug(f"Request params: project_id={project_id}")
        
        if not project_id:
            return jsonify({
                "success": False,
                "error": "Please provide project_id"
            }), 400

        # 获取项目
        project = ProjectManager.get_project(project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"Project not found: {project_id}"
            }), 404

        # 检查项目状态
        force = data.get('force', False)  # 强制重新构建
        
        if project.status == ProjectStatus.CREATED:
            return jsonify({
                "success": False,
                "error": "Ontology not yet generated for this project, call /ontology/generate first"
            }), 400
        
        if project.status == ProjectStatus.GRAPH_BUILDING and not force:
            return jsonify({
                "success": False,
                "error": "Graph build already in progress, do not resubmit. Use force: true to force rebuild",
                "task_id": project.graph_build_task_id
            }), 400
        
        # 如果强制重建，重置状态
        if force and project.status in [ProjectStatus.GRAPH_BUILDING, ProjectStatus.FAILED, ProjectStatus.GRAPH_COMPLETED]:
            project.status = ProjectStatus.ONTOLOGY_GENERATED
            project.graph_id = None
            project.graph_build_task_id = None
            project.error = None
        
        # 获取配置
        graph_name = data.get('graph_name', project.name or 'MiroFish Graph')
        chunk_size = data.get('chunk_size', project.chunk_size or Config.DEFAULT_CHUNK_SIZE)
        chunk_overlap = data.get('chunk_overlap', project.chunk_overlap or Config.DEFAULT_CHUNK_OVERLAP)
        
        # 更新项目配置
        project.chunk_size = chunk_size
        project.chunk_overlap = chunk_overlap
        
        # 获取提取的文本
        text = ProjectManager.get_extracted_text(project_id)
        if not text:
            return jsonify({
                "success": False,
                "error": "Extracted text not found"
            }), 400
        
        # 获取本体
        ontology = project.ontology
        if not ontology:
            return jsonify({
                "success": False,
                "error": "Ontology definition not found"
            }), 400
        
        # 创建异步任务
        task_manager = TaskManager()
        task_id = task_manager.create_task(f"Build graph: {graph_name}")
        logger.info(f"Graph build task created: task_id={task_id}, project_id={project_id}")
        
        # 更新项目状态
        project.status = ProjectStatus.GRAPH_BUILDING
        project.graph_build_task_id = task_id
        ProjectManager.save_project(project)
        
        # 启动后台任务
        def build_task():
            build_logger = get_logger('mirofish.build')
            try:
                build_logger.info(f"[{task_id}] Starting graph build...")
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.PROCESSING,
                    message="Initializing graph build service..."
                )
                
                # 创建图谱构建服务
                builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
                
                # 分块
                task_manager.update_task(
                    task_id,
                    message="Splitting text into chunks...",
                    progress=5
                )
                chunks = TextProcessor.split_text(
                    text, 
                    chunk_size=chunk_size, 
                    overlap=chunk_overlap
                )
                total_chunks = len(chunks)
                
                # 创建图谱
                task_manager.update_task(
                    task_id,
                    message="Creating Zep graph...",
                    progress=10
                )
                graph_id = builder.create_graph(name=graph_name)
                
                # 更新项目的graph_id
                project.graph_id = graph_id
                ProjectManager.save_project(project)
                
                # 设置本体
                task_manager.update_task(
                    task_id,
                    message="Setting ontology definition...",
                    progress=15
                )
                builder.set_ontology(graph_id, ontology)
                
                # 添加文本（progress_callback 签名是 (msg, progress_ratio)）
                def add_progress_callback(msg, progress_ratio):
                    progress = 15 + int(progress_ratio * 40)  # 15% - 55%
                    task_manager.update_task(
                        task_id,
                        message=msg,
                        progress=progress
                    )
                
                task_manager.update_task(
                    task_id,
                    message=f"Adding {total_chunks} text chunks...",
                    progress=15
                )
                
                episode_uuids = builder.add_text_batches(
                    graph_id, 
                    chunks,
                    batch_size=5,
                    progress_callback=add_progress_callback
                )
                
                # 等待Zep处理完成（查询每个episode的processed状态）
                task_manager.update_task(
                    task_id,
                    message="Waiting for Zep to process data...",
                    progress=55
                )
                
                def wait_progress_callback(msg, progress_ratio):
                    progress = 55 + int(progress_ratio * 35)  # 55% - 90%
                    task_manager.update_task(
                        task_id,
                        message=msg,
                        progress=progress
                    )
                
                builder._wait_for_episodes(episode_uuids, wait_progress_callback)
                
                # 获取图谱数据
                task_manager.update_task(
                    task_id,
                    message="Fetching graph data...",
                    progress=95
                )
                graph_data = builder.get_graph_data(graph_id)
                
                # 更新项目状态
                project.status = ProjectStatus.GRAPH_COMPLETED
                ProjectManager.save_project(project)
                
                node_count = graph_data.get("node_count", 0)
                edge_count = graph_data.get("edge_count", 0)
                build_logger.info(f"[{task_id}] Graph build complete: graph_id={graph_id}, nodes={node_count}, edges={edge_count}")
                
                # 完成
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED,
                    message="Graph build complete",
                    progress=100,
                    result={
                        "project_id": project_id,
                        "graph_id": graph_id,
                        "node_count": node_count,
                        "edge_count": edge_count,
                        "chunk_count": total_chunks
                    }
                )
                
            except Exception as e:
                # 更新项目状态为失败
                build_logger.error(f"[{task_id}] Graph build failed: {str(e)}")
                build_logger.debug(traceback.format_exc())
                
                project.status = ProjectStatus.FAILED
                project.error = str(e)
                ProjectManager.save_project(project)
                
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.FAILED,
                    message=f"Build failed: {str(e)}",
                    error=traceback.format_exc()
                )
        
        # 启动后台线程
        thread = threading.Thread(target=build_task, daemon=True)
        thread.start()
        
        return jsonify({
            "success": True,
            "data": {
                "project_id": project_id,
                "task_id": task_id,
                "message": "Graph build task started, check progress via /task/{task_id}"
            }
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 任务查询接口 ==============

@graph_bp.route('/task/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """
    查询任务状态
    """
    task = TaskManager().get_task(task_id)
    
    if not task:
        return jsonify({
            "success": False,
            "error": f"Task not found: {task_id}"
        }), 404
    
    return jsonify({
        "success": True,
        "data": task.to_dict()
    })


@graph_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """
    列出所有任务
    """
    tasks = TaskManager().list_tasks()
    
    return jsonify({
        "success": True,
        "data": [t.to_dict() for t in tasks],
        "count": len(tasks)
    })


# ============== 图谱数据接口 ==============

@graph_bp.route('/data/<graph_id>', methods=['GET'])
def get_graph_data(graph_id: str):
    """
    获取图谱数据（节点和边）
    """
    try:
        if not Config.ZEP_API_KEY:
            return jsonify({
                "success": False,
                "error": "ZEP_API_KEY not configured"
            }), 500

        builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
        graph_data = builder.get_graph_data(graph_id)
        
        return jsonify({
            "success": True,
            "data": graph_data
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/seed/extract-text', methods=['POST'])
def seed_extract_text():
    """Extract plain text from an uploaded file using FileParser (PyMuPDF for PDF)."""
    if 'file' not in request.files:
        return jsonify({"error": "file field required"}), 400
    file = request.files['file']
    if not file.filename or not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type"}), 400
    try:
        import tempfile, shutil
        suffix = os.path.splitext(file.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        text = FileParser.extract_text(tmp_path)
        os.unlink(tmp_path)
        logger.info(f"[seed/extract-text] extracted {len(text)} chars from {file.filename}")
        return jsonify({"text": text, "length": len(text)})
    except Exception as e:
        logger.error(f"[seed/extract-text] {e}")
        return jsonify({"error": str(e)}), 500


@graph_bp.route('/seed/research', methods=['POST'])
def seed_research():
    """
    STEP A of the two-step web_only flow.
    Gathers sources without synthesizing.  Returns sources preview + research_id.
    """
    from app.services.seed_agent import SeedAgent

    _cleanup_pending_research()

    data = request.get_json() or {}
    query = data.get("query", "").strip()
    simulation_requirement = data.get("simulation_requirement", "").strip()

    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        agent = SeedAgent()
        preview = agent.get_sources_preview(query, simulation_requirement)

        research_id = str(uuid.uuid4())
        pending_research[research_id] = {
            "query": query,
            "simulation_requirement": simulation_requirement,
            "raw_results": preview["raw_results"],
            "sub_queries": preview["sub_queries"],
            "created_at": time.time(),
        }

        logger.info(
            f"[seed/research] research_id={research_id}, "
            f"sources={len(preview['sources'])}, elapsed={preview['elapsed_seconds']}s"
        )

        return jsonify({
            "research_id": research_id,
            "sources": preview["sources"],
            "sub_queries": preview["sub_queries"],
            "sources_count": len(preview["sources"]),
            "elapsed_seconds": preview["elapsed_seconds"],
        })

    except Exception as e:
        logger.error(f"[seed/research] {e}")
        return jsonify({"error": str(e)}), 500


@graph_bp.route('/seed/confirm', methods=['POST'])
def seed_confirm():
    """
    STEP B of the two-step web_only flow.
    action="search_more": gather more sources, merge, return updated list.
    action="proceed": synthesize + create project (same shape as /seed).
    """
    from app.services.seed_agent import SeedAgent

    _cleanup_pending_research()

    data = request.get_json() or {}
    research_id = data.get("research_id", "")
    action = data.get("action", "proceed")

    if research_id not in pending_research:
        return jsonify({"error": "research_id not found or expired"}), 404

    entry = pending_research[research_id]
    query = entry["query"]
    simulation_requirement = entry["simulation_requirement"]

    try:
        agent = SeedAgent()

        if action == "search_more":
            new_preview = agent.get_sources_preview(query, simulation_requirement)
            existing_urls = {r["url"] for r in entry["raw_results"] if r.get("url")}
            merged = entry["raw_results"] + [
                r for r in new_preview["raw_results"]
                if r.get("url") and r["url"] not in existing_urls
            ]
            entry["raw_results"] = merged
            entry["sub_queries"] = new_preview["sub_queries"]
            pending_research[research_id] = entry

            sources = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "source": r.get("source", "")}
                for r in merged
            ]
            logger.info(f"[seed/confirm] search_more: merged={len(merged)} sources")
            return jsonify({
                "research_id": research_id,
                "sources": sources,
                "sub_queries": entry["sub_queries"],
                "sources_count": len(sources),
                "elapsed_seconds": new_preview["elapsed_seconds"],
            })

        # action == "proceed": synthesize and create project
        project_name = data.get("project_name", query[:50])
        result_markdown = agent._synthesize(query, simulation_requirement, entry["raw_results"])
        result_sources = [r["url"] for r in entry["raw_results"] if r.get("url")]

        del pending_research[research_id]

        project = ProjectManager.create_project(name=project_name)
        project.simulation_requirement = simulation_requirement

        files_dir = os.path.join(ProjectManager._get_project_dir(project.project_id), 'files')
        os.makedirs(files_dir, exist_ok=True)
        seed_path = os.path.join(files_dir, 'seed_document.md')
        with open(seed_path, 'w', encoding='utf-8') as f:
            f.write(result_markdown)
        project.files = [{"filename": "seed_document.md", "size": len(result_markdown)}]

        ProjectManager.save_extracted_text(project.project_id, result_markdown)
        project.total_text_length = len(result_markdown)

        ontology = OntologyGenerator().generate(
            document_texts=[result_markdown],
            simulation_requirement=simulation_requirement,
        )

        project.ontology = {
            "entity_types": ontology.get("entity_types", []),
            "edge_types": ontology.get("edge_types", []),
        }
        project.analysis_summary = ontology.get("analysis_summary", "")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        ProjectManager.save_project(project)

        logger.info(f"[seed/confirm] Project created: {project.project_id}")
        logger.info(f"[seed] result.markdown={len(result_markdown)} chars, sources={len(result_sources)}")

        return jsonify({
            "success": True,
            "data": {
                "project_id": project.project_id,
                "project_name": project.name,
                "ontology": project.ontology,
                "analysis_summary": project.analysis_summary,
                "files": project.files,
                "total_text_length": project.total_text_length,
                "seed_meta": {
                    "mode": "web_only",
                    "sources_count": len(result_sources),
                    "elapsed_seconds": 0,
                    "sources": result_sources[:10],
                },
            }
        })

    except Exception as e:
        logger.error(f"[seed/confirm] {e}")
        return jsonify({"error": str(e)}), 500


@graph_bp.route('/seed', methods=['POST'])
def seed_and_generate_ontology():
    """
    New endpoint for Seed Agent modes (web_only and hybrid).
    Returns same shape as /ontology/generate so frontend flow is unchanged.
    """
    from app.services.seed_agent import SeedAgent

    data = request.get_json() or {}
    query = data.get("query", "").strip()
    simulation_requirement = data.get("simulation_requirement", "").strip()
    project_name = data.get("project_name", query[:50])
    mode = data.get("mode", "web_only")  # "web_only" | "hybrid"
    file_text = data.get("file_text", "")  # only for hybrid

    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        agent = SeedAgent()

        if mode == "hybrid":
            if not file_text:
                return jsonify({"error": "file_text required for hybrid mode"}), 400
            result = agent.run_hybrid(file_text, query, simulation_requirement)
            logger.info(f"[seed] result.markdown={len(result.markdown)} chars, sources={len(result.sources)}")
        else:
            result = agent.run_web_only(query, simulation_requirement)
            logger.info(f"[seed] result.markdown={len(result.markdown)} chars, sources={len(result.sources)}")

        # From here, identical to existing /ontology/generate flow
        project = ProjectManager.create_project(name=project_name)
        project.simulation_requirement = simulation_requirement

        # Write seed document directly (not via FileStorage)
        files_dir = os.path.join(ProjectManager._get_project_dir(project.project_id), 'files')
        os.makedirs(files_dir, exist_ok=True)
        seed_path = os.path.join(files_dir, 'seed_document.md')
        with open(seed_path, 'w', encoding='utf-8') as f:
            f.write(result.markdown)
        project.files = [{"filename": "seed_document.md", "size": len(result.markdown)}]

        ProjectManager.save_extracted_text(project.project_id, result.markdown)
        project.total_text_length = len(result.markdown)

        ontology = OntologyGenerator().generate(
            document_texts=[result.markdown],
            simulation_requirement=simulation_requirement,
        )

        project.ontology = {
            "entity_types": ontology.get("entity_types", []),
            "edge_types": ontology.get("edge_types", []),
        }
        project.analysis_summary = ontology.get("analysis_summary", "")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        ProjectManager.save_project(project)

        logger.info(f"[seed] Project created: {project.project_id}, mode={mode}")

        return jsonify({
            "success": True,
            "data": {
                "project_id": project.project_id,
                "project_name": project.name,
                "ontology": project.ontology,
                "analysis_summary": project.analysis_summary,
                "files": project.files,
                "total_text_length": project.total_text_length,
                "seed_meta": {
                    "mode": mode,
                    "sources_count": len(result.sources),
                    "elapsed_seconds": result.elapsed_seconds,
                    "sources": result.sources[:10],
                },
            }
        })

    except Exception as e:
        logger.error(f"Seed agent error: {e}")
        return jsonify({"error": str(e)}), 500


@graph_bp.route('/delete/<graph_id>', methods=['DELETE'])
def delete_graph(graph_id: str):
    """
    删除Zep图谱
    """
    try:
        if not Config.ZEP_API_KEY:
            return jsonify({
                "success": False,
                "error": "ZEP_API_KEY not configured"
            }), 500

        builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
        builder.delete_graph(graph_id)

        return jsonify({
            "success": True,
            "message": f"Graph deleted: {graph_id}"
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500
