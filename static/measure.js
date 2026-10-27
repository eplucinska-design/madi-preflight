// Millimetre rulers, PDF page boxes and a measuring tool drawn over the rendered page.
const boxStyles={mediabox:['MediaBox','#9aa0aa','4 3'],bleedbox:['BleedBox','#4c8dff',''],trimbox:['TrimBox','#2fbf87','']};
const view={boxes:true,measure:false};
try{Object.assign(view,{boxes:localStorage.getItem('madi-show-boxes')!=='false'});}catch{}
const svgNS='http://www.w3.org/2000/svg';
const overlay=document.createElementNS(svgNS,'svg');overlay.classList.add('page-overlay');overlay.setAttribute('preserveAspectRatio','none');$('page-wrap').append(overlay);
const measureLayer=document.createElementNS(svgNS,'g');
const measureLabel=document.createElement('div');measureLabel.className='measure-label';measureLabel.hidden=true;$('page-wrap').append(measureLabel);
const rulerTop=document.createElement('canvas'),rulerLeft=document.createElement('canvas'),rulerCorner=document.createElement('div');
rulerTop.className='ruler ruler-top';rulerLeft.className='ruler ruler-left';rulerCorner.className='ruler-corner';rulerCorner.textContent='mm';
$('canvas').before(rulerTop);$('canvas').before(rulerLeft);$('canvas').before(rulerCorner);
const info=()=>active()?.info;
// Rulers start at the TrimBox corner when the PDF defines one, otherwise at the visible page corner.
function origin(){const box=info()?.boxes;return box?{x:box.trimbox[0],y:box.trimbox[1]}:{x:0,y:0};}
function scale(){const doc=info();return doc?$('page-wrap').clientWidth/doc.width:0;}
function drawBoxes(){overlay.replaceChildren();const doc=info();if(!doc?.boxes)return;overlay.setAttribute('viewBox',`0 0 ${doc.width} ${doc.height}`);const crop=doc.boxes.cropbox;if(view.boxes)for(const[name,[label,color,dash]]of Object.entries(boxStyles)){const box=doc.boxes[name];
// Boxes equal to the visible page carry no information; skip them.
if(!box||box.every((value,index)=>Math.abs(value-crop[index])<0.05))continue;const rect=document.createElementNS(svgNS,'rect');rect.setAttribute('x',box[0]);rect.setAttribute('y',box[1]);rect.setAttribute('width',box[2]-box[0]);rect.setAttribute('height',box[3]-box[1]);rect.setAttribute('stroke',color);rect.setAttribute('stroke-dasharray',dash);rect.dataset.box=label;const title=document.createElementNS(svgNS,'title');title.textContent=`${label}: ${(box[2]-box[0]).toFixed(2)} × ${(box[3]-box[1]).toFixed(2)} mm`;rect.append(title);overlay.append(rect);}overlay.append(measureLayer);}
function step(pxPerMm){for(const value of [1,2,5,10,20,50,100,200,500])if(value*pxPerMm>=48)return value;return 1000;}
function drawRuler(canvas,horizontal){const doc=info(),ratio=devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);const style=getComputedStyle(document.body);ctx.fillStyle=style.getPropertyValue('--panel');ctx.fillRect(0,0,rect.width,rect.height);if(!doc||$('page-wrap').hidden)return;const page=$('page-wrap').getBoundingClientRect(),k=scale(),o=origin();const start=horizontal?page.left-rect.left+o.x*k:page.top-rect.top+o.y*k;const length=horizontal?rect.width:rect.height,size=horizontal?rect.height:rect.width;const major=step(k),minor=major/(major>=10&&major%10===0?10:5);ctx.strokeStyle=ctx.fillStyle=style.getPropertyValue('--muted');ctx.font='9px system-ui,sans-serif';ctx.lineWidth=1;ctx.beginPath();const first=Math.floor(-start/k/minor)*minor;for(let mm=first;mm*k+start<=length;mm+=minor){const pos=Math.round(mm*k+start)+.5,isMajor=Math.abs(mm/major-Math.round(mm/major))<1e-6,tick=isMajor?size*.6:size*.25;if(horizontal){ctx.moveTo(pos,size);ctx.lineTo(pos,size-tick);}else{ctx.moveTo(size,pos);ctx.lineTo(size-tick,pos);}if(isMajor){const text=String(Math.round(mm));if(horizontal)ctx.fillText(text,pos+2,9);else{ctx.save();ctx.translate(9,pos+2);ctx.rotate(-Math.PI/2);ctx.textAlign='right';ctx.fillText(text,0,0);ctx.restore();}}}ctx.stroke();}
function drawRulers(){drawRuler(rulerTop,true);drawRuler(rulerLeft,false);}
function refreshOverlay(){drawBoxes();drawRulers();}
const baseApplyZoom=applyZoom;applyZoom=function(){baseApplyZoom();clearMeasure();refreshOverlay();};
$('canvas').addEventListener('scroll',drawRulers);addEventListener('resize',drawRulers);new ResizeObserver(drawRulers).observe($('canvas'));
// Toolbar: page boxes and measuring tool.
function toolbarButton(id,glyph,title,onclick){const button=document.createElement('button');button.id=id;button.className='icon-button';button.title=title;button.setAttribute('aria-label',title);button.append(icon(glyph));button.onclick=onclick;return button;}
const boxesButton=toolbarButton('boxes-toggle','frame','Pola stron: TrimBox zielony, BleedBox niebieski, MediaBox szary (B)',()=>{view.boxes=!view.boxes;try{localStorage.setItem('madi-show-boxes',view.boxes);}catch{}boxesButton.classList.toggle('active',view.boxes);drawBoxes();});
const measureButton=toolbarButton('measure-tool','ruler','Pomiar odległości w mm (M)',()=>setMeasure(!view.measure));
boxesButton.classList.toggle('active',view.boxes);
const measureGroup=document.createElement('div');measureGroup.className='doc-toolbar-group';measureGroup.append(measureButton,boxesButton);
document.querySelector('.doc-toolbar-group').after(measureGroup);icons();
function setMeasure(on){view.measure=on;measureButton.classList.toggle('active',on);$('page-wrap').classList.toggle('measuring',on);if(on){$('select-tool').classList.remove('active');$('hand-tool').classList.remove('active');}else{clearMeasure();$('select-tool').click();}}
$('select-tool').addEventListener('click',()=>view.measure&&setMeasure(false));$('hand-tool').addEventListener('click',()=>{if(view.measure){view.measure=false;measureButton.classList.remove('active');$('page-wrap').classList.remove('measuring');clearMeasure();}});
function clearMeasure(){measureLayer.replaceChildren();measureLabel.hidden=true;}
function pointMm(event){const rect=$('page-wrap').getBoundingClientRect(),k=scale();return{x:(event.clientX-rect.left)/k,y:(event.clientY-rect.top)/k};}
function drawMeasure(a,b){const k=scale();measureLayer.replaceChildren();const line=document.createElementNS(svgNS,'line');line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y);line.classList.add('measure-line');measureLayer.append(line);for(const p of [a,b]){const dot=document.createElementNS(svgNS,'circle');dot.setAttribute('cx',p.x);dot.setAttribute('cy',p.y);dot.setAttribute('r',3/k);dot.classList.add('measure-dot');measureLayer.append(dot);}const dx=b.x-a.x,dy=b.y-a.y,o=origin();measureLabel.innerHTML=`<b>${Math.hypot(dx,dy).toFixed(2)} mm</b><span>Δx ${Math.abs(dx).toFixed(2)} · Δy ${Math.abs(dy).toFixed(2)}</span><span>od ${(a.x-o.x).toFixed(1)}, ${(a.y-o.y).toFixed(1)}</span>`;measureLabel.style.left=b.x*k+12+'px';measureLabel.style.top=b.y*k+12+'px';measureLabel.hidden=false;}
let measureStart=null;
$('page-wrap').addEventListener('pointerdown',event=>{if(!view.measure||event.button!==0)return;event.preventDefault();event.stopPropagation();$('page-wrap').setPointerCapture(event.pointerId);measureStart=pointMm(event);let end=measureStart;
// Shift snaps to horizontal or vertical, as in DTP applications.
const move=e=>{end=pointMm(e);if(e.shiftKey){if(Math.abs(end.x-measureStart.x)>Math.abs(end.y-measureStart.y))end.y=measureStart.y;else end.x=measureStart.x;}drawMeasure(measureStart,end);};const up=()=>{$('page-wrap').removeEventListener('pointermove',move);measureStart=null;};$('page-wrap').addEventListener('pointermove',move);$('page-wrap').addEventListener('pointerup',up,{once:true});},true);
$('page-wrap').addEventListener('click',event=>{if(view.measure)event.stopPropagation();},true);
document.addEventListener('keydown',event=>{if(event.target.closest?.('input,select,textarea')||event.ctrlKey||event.metaKey||event.altKey)return;if(event.key==='m'||event.key==='M')setMeasure(!view.measure);else if(event.key==='b'||event.key==='B')boxesButton.click();else if(event.key==='Escape'&&view.measure)clearMeasure();});
