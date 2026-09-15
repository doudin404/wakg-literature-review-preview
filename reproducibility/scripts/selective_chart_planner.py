"""One Sol pass: choose useful geometry or return direct values in that pass."""
from pathlib import Path
from PIL import Image
from source_quantity_producer import invoke,obj,atomic_json
from pdf_thread_access import serialized_pdf

DIRECT_PROMPT='''读取所给面板，保留owner_key、property、age_seconds、原图像素定位。
单个读数写direct_result.values，每项含value、unit、bbox_px、series、category；value是所需物理量本身，峰位取横坐标，强度取纵坐标。
连续谱线写series，每项含label、owner_key、points:[[x,y],...]、pixel_points、gap_before_indices。
保留整条可读轨迹，在峰、拐点附近增加点；遮挡处断开并用review_regions标出范围。
纵轴没有数字刻度时，以该面板可见绘图区的上下边界定义0到1的相对强度，y_axis.unit写relative intensity，注明这个基准。峰位标签另存values。
面板含kind、plot_bbox_px、x_axis/y_axis:{scale,anchors,unit}；PSD含curve_type，NMR含isotope。
数值标签写approximate:false，估读写approximate:true。只读取所给图，保持系列与材料或试样对应。'''

PROMPT='''逐面板选择且只选择一种read_mode：script或direct。
script：只返回系列归属、坐标轴、扫描区域与工具参数，数值和曲线点集由脚本读取。
direct：不适合脚本或已有清晰数字标签时直接返回估读值或曲线点集。混合图按可独立读取的面板分开选择。
如提供study_context，每条series、bar及直接value都附owner_key（从提供目录选）、property、age_seconds（未知null）、specimen、method；同名试样按目录的specimen_type和所给试验段落区分，series/category保留图例或分组标签。
PSD注明curve_type为cumulative_finer或reported_volume_density；NMR注明isotope为29Si或27Al。
同一材料、试验条件和体积/质量/数量基准的PSD若同时画累计曲线与差分柱图，完整提取累计曲线及D10/D50/D90，差分柱图仅作为原图核对，不另建扫描面板。仅有差分图或表达不同数据时照常提取。不同系列或条件不可合并；同一分布用相同distribution_id标识。
需要单独记录的印刷峰位标签作为direct面板，与扫描轨迹分开。
脚本适用范围：提供的PDF路径中可分离的完整曲线；可隔离的单条连续栅格曲线；上下分开且各自连续的黑色FTIR谱线；清晰无标签彩色实心柱；可隔离的分离散点。
彩色填充直方图可设 geometry:histogram_raster，单独列一个面板及 series 的材料归属；专用流程会读取柱顶并检查叠加图。共用绘图区的累计曲线另列面板，使用自己的纵轴。
其他情况（包括堆叠柱、饼图、密集/重叠栅格系列）直接返回读数。
返回panels_json为JSON数组。所有面板含kind、page、figure；直接读的面板含direct_result:{values:[{series,category,x,y,unit}],note}，无值坐标为null。
脚本曲线含plot_bbox_px、x_axis/y_axis:{scale:linear或log10,anchors:[[像素,值],[像素,值]],unit}、series:[{label}]。
矢量曲线设geometry:pdf_vector，每个series引用下面清单中的source_path_indices；路径坐标属于原页面图片像素。保留整个路径，不转成五个代表点。
栅格单曲线设isolated_single_curve:true、plot_area_clean:true，每个series含rgb与color_distance；exclude_boxes_px列出绘图区内图例/文字框，边框排除在绘图区外。
上下分开的黑色FTIR谱线设geometry:separated_ftir_raster；每条series给trace_bbox_px，完整包住本条轨迹但避开其他谱线、轴框和文字，区域彼此分开。给共同x_axis/y_axis、label、owner_key、property即可，点集由脚本读取。重叠、遮挡或明显断线的谱线仍直接估读。
普通柱设clear_solid_bars:true、value_axis、baseline_pixel、baseline_tolerance_px:3、endpoint_mode:absolute_chromatic_top、bars:[{label,bbox_px,rgb,color_distance:25,minimum_row_coverage:0.6}]。
散点设separated_markers:true、plot_area_clean:true、plot_bbox_px、exclude_boxes_px、x_axis/y_axis、series:[{label,rgb,color_distance:25}]。
kind可为curve/psd/ftir/xrd/nmr/bar/scatter或实际类型。所有坐标使用原图像素，颜色0到255。保留材料、试样与单位；估读值注明约。只依据所给图片及路径清单。
'''+ '\n以下数值/点集输出格式仅适用于read_mode=direct：\n'+DIRECT_PROMPT


def exclusive_panels(panels):
    """Keep one authoritative reading route per panel."""
    import copy
    from selective_chart_policy import backend
    result=copy.deepcopy(panels)
    for p in result:
        mode=p.get('read_mode') or ('direct' if backend(p)=='agent' else 'script')
        p['read_mode']=mode
        if mode=='script':
            p.pop('direct_result',None)
            for s in p.get('series',[]):
                for key in ('points','pixel_points','gap_before_indices'):s.pop(key,None)
    return result

def incomplete_curves(panels):
    from selective_chart_policy import backend
    from psd_representation import prefer_cumulative
    panels=prefer_cumulative(panels)
    missing=[]
    for i,p in enumerate(panels):
        if p.get('skip_auxiliary_psd'):continue
        if p['kind'] not in {'psd','ftir','xrd','nmr'} or backend(p)!='agent':continue
        series=p.get('series',[])+(p.get('direct_result') or {}).get('series',[])
        complete={(s.get('owner_key'),s.get('label',s.get('series'))) for s in series if s.get('points')}
        if not complete or any((s.get('owner_key'),s.get('label',s.get('series'))) not in complete for s in series):
            missing.append(i)
    return missing

@serialized_pdf
def inventory(pdf,page_no,image):
    if not pdf:return []
    import fitz
    with fitz.open(pdf) as doc:
        page=doc[page_no-1]
        with Image.open(image) as im:sx,sy=im.width/page.rect.width,im.height/page.rect.height
        result=[]
        for i,d in enumerate(page.get_drawings()):
            if len(d['items'])<15 or not d.get('color'):continue
            result.append(dict(id=i,bbox_px=[round(v*(sx if j%2==0 else sy),2) for j,v in enumerate(d['rect'])],
                               rgb=[round(v*255) for v in d['color']],segments=len(d['items'])))
        return result

def plan(image,pdf,page,caption,output,context=None):
    job=dict(source_id='figure-1',image=image,pdf=pdf,page=page,caption=caption,
        folder=Path(output).parent,task=(context or {}).get('task',{}),
        methods=(context or {}).get('methods',[]),bbox_px=(context or {}).get('figure_bbox_px'))
    return plan_scan_batch([job],(context or {}).get('owners',[]),output)['figure-1']


def plan_scan_batch(jobs,owners,output):
    """Current entry: caption task routes to the shared scan-first tool."""
    from chart_scan_service import read_image
    from chart_image_input import pack,translate
    bindings={}
    for job in jobs:
        folder=Path(output)/job['source_id']
        image,mappings=pack([job],folder/'input')
        panels=read_image(image,dict(task=job.get('task',{}),caption=job.get('caption',''),
            owners=owners,methods=job.get('methods',[])),folder,call=invoke)
        dx,dy=mappings[job['source_id']]['offset_to_page']
        for p in panels:p.setdefault('page',job['page']);p.setdefault('figure',job.get('caption',''))
        binding=dict(pdf=str(job['pdf']) if job.get('pdf') else None,page=job['page'],
            panels=[translate(p,dx,dy) for p in panels],input_mapping=mappings[job['source_id']])
        atomic_json(Path(job['folder'])/'planner/binding.json',binding)
        bindings[job['source_id']]=binding
    return bindings


def plan_batch(jobs,owners,output):
    return plan_scan_batch(jobs,owners,output)

def legacy_plan_batch(jobs, owners, output):
    """Compatibility name for the current scan-first planner.

    The former implementation sent figure crops to an Agent that could write
    curve points into the planning binding.  Keeping that implementation
    callable would reintroduce the superseded pre-scan estimation route.
    """
    return plan_scan_batch(jobs,owners,output)
