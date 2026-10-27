// Montax-style shell: ribbon with grouped tools, parameter dock left, sheet centre, control dock right.
// Rearranges the existing working elements; every ribbon button calls an implemented function.
document.body.classList.add('shell');
const shellField=key=>$('field-'+key);
function setField(key,value){const el=shellField(key);if(!el)return;if(el.type==='checkbox')el.checked=value;else el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
function imposerWith(values){openTool('imposer');for(const[key,value]of Object.entries(values))setField(key,value);}
const needsDoc=()=>{if(!active()){status('Najpierw otwórz dokument PDF.',true);return true;}return false;};
const ribbonTabs=[
['Dokument',[['Plik',[['folder-open','Otwórz PDF',()=>$('file').click()],['download','Pobierz wynik',()=>$('export').click(),()=>!active()?.result]]],['Wynik',[['columns-2','Porównaj',()=>openCompare(),()=>!active()?.result],['repeat','Pracuj na wyniku',()=>{$('input-source').value='result';$('input-source').dispatchEvent(new Event('change'));status('Następna operacja użyje poprzedniego wyniku.');},()=>!active()?.result]]],['Automatyzacja',[['list-ordered','Kolejka',()=>setPanel('workflow')],['save','Presety',()=>setPanel('presets')]]]]],
['Kolory',[['Kolory spot',[['tag','Nazwa / zamiana','spot_rename'],['blend','Nadruk','spot_overprint'],['sliders-horizontal','Składowe CMYK','spot_alternate']]],['Konwersja',[['arrow-right-left','Spot → CMYK','spot_to_cmyk'],['droplet','CMYK → spot','cmyk_to_spot'],['eraser','Nadruk bieli','white_overprint_fix']]],['Kolory',[['contrast','Czerń','black'],['pipette','Usuń kolor','remove']]]]],
['Separacje',[['Wydzielanie',[['layers-2','Wydziel spot','spot_extract'],['paint-bucket','WHITE','white'],['layers','HOT / lakier','matrix']]],['Technologia',[['scissors','Linie techn.','dieline']]]]],
['Format i spady',[['Format',[['crop','Format i spad','bleed'],['scan-line','Wg wykrojnika','format_dieline'],['square-dashed','Ustaw pola strony','boxes']]],['Widok',[['frame','Pola stron',()=>$('boxes-toggle').click()],['ruler','Pomiar',()=>$('measure-tool').click()]]]]],
['Strony',[['Strony',[['files','Układ stron','pages'],['file-minus','Usuń strony','delete_pages'],['file-plus','Puste strony','insert_blank']]],['Pliki',[['combine','Połącz PDF',()=>openMerge()],['split','Podziel PDF',()=>openSplit()]]]]],
['Montaż',[['Montaż',[['layout-grid','Siatka',()=>imposerWith({imposer_order:'repeat'})],['copy','Komplety',()=>imposerWith({imposer_order:'collate'})],['layers-3','Cut & stack',()=>imposerWith({imposer_order:'cut_stack'})]]],['Arkusz',[['flip-horizontal','Przód / tył',()=>{openTool('imposer');setField('imposer_duplex',!shellField('imposer_duplex')?.checked);}],['crop','Znaczniki',()=>{openTool('imposer');setField('imposer_crop_marks',!shellField('imposer_crop_marks')?.checked);}]]]]],
['Znaczniki',[['Znaczniki',[['eraser','Usuń znaczniki','cleanup'],['crop','Znaki cięcia (montaż)',()=>imposerWith({imposer_crop_marks:true})]]]]],
['Wykrojnik',[['Wykrojnik',[['spline','Generuj','shape'],['scissors','Cięcie i bigowanie','dieline'],['scan-line','Format wg wykrojnika','format_dieline']]],['Obrysy',[['pen-line','Grubość obrysów','stroke_width']]]]],
['Kontrola',[['Kontrola',[['scan-search','Analizuj stronę',()=>{if(!needsDoc())$('inspect').click();}],['columns-2','Porównaj',()=>openCompare(),()=>!active()?.result]]]]],
['Eksport',[['Eksport',[['download','Pobierz PDF',()=>$('export').click(),()=>!active()?.result],['list-ordered','Kolejka',()=>setPanel('workflow')]]]]]];
const ribbon=document.createElement('div');ribbon.className='ribbon';
const tabRow=document.createElement('div');tabRow.className='ribbon-tabs';tabRow.setAttribute('role','tablist');
const groupsRow=document.createElement('div');groupsRow.className='ribbon-groups';
const toolTab={};const disabledChecks=[];
ribbonTabs.forEach(([name,groups],index)=>{const tab=document.createElement('button');tab.textContent=name;tab.setAttribute('role','tab');tab.dataset.tab=index;tab.onclick=()=>showTab(index);tabRow.append(tab);const pane=document.createElement('div');pane.className='ribbon-pane';pane.dataset.tab=index;for(const[label,buttons]of groups){const group=document.createElement('div');group.className='ribbon-group';const row=document.createElement('div');row.className='ribbon-buttons';for(const[glyph,text,action,disabled]of buttons){const button=document.createElement('button');button.className='ribbon-button';button.append(icon(glyph),Object.assign(document.createElement('span'),{textContent:text}));if(typeof action==='string'){toolTab[action]=index;button.dataset.tool=action;button.title=toolInfo[action]?.[0]??text;button.onclick=()=>openTool(action);}else{button.title=text;button.onclick=action;}if(disabled)disabledChecks.push([button,disabled]);row.append(button);}const caption=document.createElement('div');caption.className='ribbon-caption';caption.textContent=label;group.append(row,caption);pane.append(group);}groupsRow.append(pane);});
ribbon.append(tabRow,groupsRow);
const queueCount=$('queue-count');document.querySelector('.modulebar').replaceWith(ribbon);
let ribbonTab=0;try{const savedTab=localStorage.getItem('madi-ribbon-tab');ribbonTab=savedTab==='fav'?'fav':Number(savedTab)||0;}catch{}
function showTab(index){ribbonTab=index;tabRow.querySelectorAll('button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===String(index))));groupsRow.querySelectorAll('.ribbon-pane').forEach(p=>p.hidden=p.dataset.tab!==String(index));try{localStorage.setItem('madi-ribbon-tab',index);}catch{}}
function refreshRibbon(){for(const[button,check]of disabledChecks)button.disabled=busy||check();groupsRow.querySelectorAll('.ribbon-button[data-tool]').forEach(b=>b.classList.toggle('active',b.dataset.tool===mode&&panel==='parameters'));}
// Docks: parameters left, pages + control + colours right.
const rightDock=document.createElement('aside');rightDock.className='right-dock';rightDock.setAttribute('aria-label','Panele kontroli');
function dockPanel(title,content,open=true){const details=document.createElement('details');details.className='dock-panel';details.open=open;const summary=document.createElement('summary');summary.textContent=title;details.append(summary,content);rightDock.append(details);return details;}
const pagesAside=document.querySelector('.pages-panel');
const pagesBody=document.createElement('div');pagesBody.className='dock-pages';pagesBody.append($('page-list'));
const pagesSummary=dockPanel('Strony ',pagesBody).querySelector('summary');pagesSummary.append($('pages-total'));
const inspection=$('panel-inspection');inspection.hidden=false;inspection.removeAttribute('id');inspection.id='panel-inspection-dock';
dockPanel('Kontrola strony',inspection);
const colourBox=document.createElement('div');colourBox.className='dock-colours';
dockPanel('Kolory spot',colourBox);
pagesAside.remove();
// Thumbnails and tools no longer share a panel, so there is nothing to switch.
showLibrary=function(){};
document.querySelector('.desk').append(rightDock);
document.querySelector('.tool-rail').remove();
document.querySelector('.panel-tabs').hidden=true;
// setPanel only knows sections inside the left dock now; inspection lives in the right dock.
const shellBaseSetPanel=setPanel;
setPanel=function(name){if(name==='inspection'||name==='tools')name='parameters';shellBaseSetPanel(name);refreshRibbon();};
async function refreshColours(){const doc=active();const file=doc&&(doc.view==='result'?doc.result:doc.file);colourBox.replaceChildren();if(!file){colourBox.textContent='Brak dokumentu.';return;}colourBox.textContent='Odczytywanie…';try{const{spots}=await spotReport(file);if(active()!==doc)return;colourBox.replaceChildren();if(!spots.length){colourBox.textContent='Brak kolorów spot.';return;}for(const spot of spots){const row=document.createElement('button');row.className='spot-row';row.title=`Strony: ${spot.pages.join(', ')||'—'} · zastępczy: ${spot.alternate||'—'} · kliknij, aby ustawić nadruk`;row.innerHTML='<span class="spot-chip"></span><span class="spot-name"></span><span class="spot-count"></span><span class="spot-op"></span>';row.querySelector('.spot-name').textContent=spot.name;row.querySelector('.spot-count').textContent=`${spot.fill}/${spot.stroke}`;row.querySelector('.spot-op').textContent=[spot.overprint_fill?'W':'',spot.overprint_stroke?'O':''].join('')||'—';row.onclick=()=>{openTool('spot_overprint');setField('spot_name',spot.name);};colourBox.append(row);}}catch(error){colourBox.textContent=error.message;}}
let colourKey=null;
const shellBaseChrome=updateDocumentChrome;
updateDocumentChrome=function(){shellBaseChrome();refreshRibbon();const doc=active();const key=doc?(doc.view==='result'?doc.result:doc.file):null;if(key!==colourKey){colourKey=key;refreshColours();}};
const shellBaseRenderTool=renderTool;
renderTool=function(key){shellBaseRenderTool(key);if(key in toolTab&&ribbonTab!=='fav'&&toolTab[key]!==ribbonTab&&!ribbon.contains(document.activeElement))showTab(toolTab[key]);refreshRibbon();};
const shellBaseLock=lock;
lock=function(value){shellBaseLock(value);refreshRibbon();};
// Dock toggles at the right end of the ribbon tabs.
const dockState={left:true,right:true};try{Object.assign(dockState,JSON.parse(localStorage.getItem('madi-docks')||'{}'));}catch{}
function applyDocks(){document.body.classList.toggle('dock-left-off',!dockState.left);document.body.classList.toggle('dock-right-off',!dockState.right);leftDockButton.classList.toggle('active',dockState.left);rightDockButton.classList.toggle('active',dockState.right);try{localStorage.setItem('madi-docks',JSON.stringify(dockState));}catch{}}
function dockButton(glyph,title,side){const b=document.createElement('button');b.className='icon-button';b.title=title;b.setAttribute('aria-label',title);b.append(icon(glyph));b.onclick=()=>{dockState[side]=!dockState[side];applyDocks();};return b;}
const leftDockButton=dockButton('panel-left','Panel parametrów ( [ )','left'),rightDockButton=dockButton('panel-right','Panel kontroli ( ] )','right');
const spacer=document.createElement('span');spacer.className='spacer';
const queueChip=document.createElement('button');queueChip.className='queue-chip';queueChip.append(icon('list-ordered'),document.createTextNode('Kolejka '),queueCount);queueChip.onclick=()=>setPanel('workflow');
tabRow.append(spacer,queueChip,leftDockButton,rightDockButton);
// Favourites: right-click any ribbon button to pin or unpin it; pinned buttons get their own first tab.
let pins=[];try{pins=JSON.parse(localStorage.getItem('madi-ribbon-pins')||'[]');}catch{}
const allButtons=[...groupsRow.querySelectorAll('.ribbon-button')];allButtons.forEach((button,i)=>button.dataset.pin=button.dataset.tool||('cmd'+i+':'+button.textContent));
const favTab=document.createElement('button');favTab.setAttribute('role','tab');favTab.dataset.tab='fav';favTab.append(icon('star'),document.createTextNode(' Ulubione'));favTab.onclick=()=>showTab('fav');tabRow.prepend(favTab);
const favPane=document.createElement('div');favPane.className='ribbon-pane';favPane.dataset.tab='fav';groupsRow.prepend(favPane);
function renderFavourites(){favPane.replaceChildren();const group=document.createElement('div');group.className='ribbon-group';const row=document.createElement('div');row.className='ribbon-buttons';for(const pin of pins){const source=allButtons.find(b=>b.dataset.pin===pin);if(!source)continue;const copy=source.cloneNode(true);copy.onclick=()=>source.click();copy.addEventListener('contextmenu',e=>{e.preventDefault();togglePin(pin);});const check=disabledChecks.find(([b])=>b===source);if(check)disabledChecks.push([copy,check[1]]);row.append(copy);}const caption=document.createElement('div');caption.className='ribbon-caption';caption.textContent=pins.length?'Prawy klik: odepnij':'Prawy klik na dowolnym przycisku wstążki przypina go tutaj';group.append(row,caption);favPane.append(group);allButtons.forEach(b=>b.classList.toggle('pinned',pins.includes(b.dataset.pin)));refreshRibbon();}
function togglePin(pin){pins=pins.includes(pin)?pins.filter(p=>p!==pin):[...pins,pin];try{localStorage.setItem('madi-ribbon-pins',JSON.stringify(pins));}catch{}renderFavourites();status(pins.includes(pin)?'Przypięto do Ulubionych.':'Odpięto z Ulubionych.');}
allButtons.forEach(button=>{button.title+=' · prawy klik: przypnij do Ulubionych';button.addEventListener('contextmenu',e=>{e.preventDefault();togglePin(button.dataset.pin);});});
renderFavourites();
// Expanded ribbon: every group of every tab visible at once.
let expanded=false;try{expanded=localStorage.getItem('madi-ribbon-expanded')==='true';}catch{}
const expandButton=document.createElement('button');expandButton.className='icon-button';expandButton.title='Rozwiń wstążkę: wszystkie funkcje naraz';expandButton.setAttribute('aria-label','Rozwiń wstążkę');expandButton.append(icon('chevrons-down'));
groupsRow.querySelectorAll('.ribbon-pane').forEach(pane=>{const label=document.createElement('div');label.className='ribbon-pane-label';label.textContent=pane.dataset.tab==='fav'?'Ulubione':ribbonTabs[pane.dataset.tab][0];pane.prepend(label);});
function applyExpanded(){ribbon.classList.toggle('expanded',expanded);expandButton.classList.toggle('active',expanded);expandButton.title=expanded?'Zwiń wstążkę':'Rozwiń wstążkę: wszystkie funkcje naraz';showTab(ribbonTab);try{localStorage.setItem('madi-ribbon-expanded',expanded);}catch{}}
expandButton.onclick=()=>{expanded=!expanded;applyExpanded();};spacer.after(expandButton);
const baseShowTab=showTab;showTab=function(index){baseShowTab(index);if(expanded)groupsRow.querySelectorAll('.ribbon-pane').forEach(p=>p.hidden=false);};
applyExpanded();
shortcuts['[']=()=>leftDockButton.click();shortcuts[']']=()=>rightDockButton.click();
showTab(toolTab[mode]??ribbonTab);applyDocks();icons();setPanel(panel==='tools'?'parameters':panel);updateDocumentChrome();
