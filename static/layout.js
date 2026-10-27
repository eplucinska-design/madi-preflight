// Collapsible, resizable side panels and keyboard shortcuts; layout only, no PDF logic.
const layoutKey='madi-layout-v1';
const layout=(()=>{try{return Object.assign({left:220,right:330,leftCollapsed:false,rightCollapsed:false},JSON.parse(localStorage.getItem(layoutKey)||'{}'));}catch{return{left:220,right:330,leftCollapsed:false,rightCollapsed:false};}})();
function saveLayout(){try{localStorage.setItem(layoutKey,JSON.stringify(layout));}catch{}}
function applyLayout(){const root=document.documentElement.style;root.setProperty('--left',layout.left+'px');root.setProperty('--right',layout.right+'px');document.body.classList.toggle('left-collapsed',layout.leftCollapsed);document.body.classList.toggle('right-collapsed',layout.rightCollapsed);leftToggle.classList.toggle('active',!layout.leftCollapsed);rightToggle.classList.toggle('active',!layout.rightCollapsed);}
function layoutToggle(id,glyph,title,side){const button=document.createElement('button');button.id=id;button.className='icon-button layout-toggle';button.title=title;button.setAttribute('aria-label',title);button.append(icon(glyph));button.onclick=()=>{layout[side+'Collapsed']=!layout[side+'Collapsed'];applyLayout();saveLayout();};return button;}
const leftToggle=layoutToggle('toggle-left','panel-left','Panel narzędzi i stron ( [ )','left');
const rightToggle=layoutToggle('toggle-right','panel-right','Panel właściwości ( ] )','right');
document.querySelector('.modulebar .spacer').after(leftToggle,rightToggle);
function resizer(panel,side,min,max){const handle=document.createElement('div');handle.className='panel-resizer';handle.title='Przeciągnij, aby zmienić szerokość';panel.append(handle);handle.addEventListener('pointerdown',event=>{event.preventDefault();handle.setPointerCapture(event.pointerId);handle.classList.add('dragging');const startX=event.clientX,start=layout[side];
// In imposition the properties panel sits left of the sheets, so it grows to the right.
const direction=side==='left'||document.body.classList.contains('imposing')?1:-1;
const move=e=>{layout[side]=Math.round(Math.min(max,Math.max(min,start+(e.clientX-startX)*direction)));applyLayout();};const up=()=>{handle.classList.remove('dragging');handle.removeEventListener('pointermove',move);saveLayout();};handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',up,{once:true});});handle.addEventListener('dblclick',()=>{layout[side]=side==='left'?220:330;applyLayout();saveLayout();});}
resizer(document.querySelector('.pages-panel'),'left',150,420);
resizer($('properties'),'right',260,560);
applyLayout();icons();
const shortcuts={'[':()=>leftToggle.click(),']':()=>rightToggle.click(),v:()=>$('select-tool').click(),h:()=>$('hand-tool').click(),PageUp:()=>$('prev').click(),PageDown:()=>$('next').click()};
const ctrlShortcuts={'=':'zoom-in-rail','+':'zoom-in-rail','-':'zoom-out-rail','0':'fit-rail',Enter:'process'};
document.addEventListener('keydown',event=>{const typing=event.target.closest?.('input,select,textarea,[contenteditable=true]');if((event.ctrlKey||event.metaKey)&&ctrlShortcuts[event.key]){const target=$(ctrlShortcuts[event.key]);if(target&&!target.disabled){event.preventDefault();target.click();}return;}if(typing||event.ctrlKey||event.metaKey||event.altKey)return;const action=shortcuts[event.key];if(action){event.preventDefault();action();}});
for(const[id,hint]of [['select-tool','V'],['hand-tool','H'],['zoom-in-rail','Ctrl +'],['zoom-out-rail','Ctrl −'],['fit-rail','Ctrl 0'],['prev','PageUp'],['next','PageDown'],['process','Ctrl Enter'],['open','Ctrl O']]){const el=$(id);if(el)el.title=(el.title||el.textContent.trim())+' ('+hint+')';}
