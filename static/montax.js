// Imposition view laid out after the Montax Imposer reference recordings: compact titled panels in two
// columns on the left, the "margins and spacing" panel as a horizontal strip above the sheets.
const montaxGroups=[
['Wygląd impozycji',/^imposer_(order|rows|columns|duplex|duplex_pairing|mirror_back|add_blanks|creep_mm)$/],
['Rozmiar arkusza',/^imposer_sheet/],
['Skala i położenie',/^imposer_(fit_mode|scale_percent|rotation|align)$/],
['Znaczniki',/^imposer_(crop_marks|mark_|draw_|registration_marks)/],
['Powtarzanie stron',/^imposer_repeat_count$/],
['Marginesy i odstępy impozycji',/^imposer_(margin_|gap_|bleed_mm)/]];
const montaxBaseGroupFor=groupFor;
groupFor=function(key){if(key.startsWith('imposer_'))for(const[name,pattern]of montaxGroups)if(pattern.test(key))return name;return montaxBaseGroupFor(key);};
// Fields in the strip live outside #fields, so reading, previewing and locking include it explicitly.
const montaxStrip=document.createElement('div');montaxStrip.className='montax-strip';montaxStrip.hidden=true;
document.querySelector('.imposition-workspace').prepend(montaxStrip);
const montaxBaseReadFields=readFields;
readFields=function(){const data=montaxBaseReadFields();if(mode==='imposer'&&active())data.imposer_job_name=active().file.name;montaxStrip.querySelectorAll('input,select').forEach(el=>data[el.name]=el.type==='checkbox'?el.checked:el.value);return data;};
montaxStrip.addEventListener('input',()=>{if(mode==='imposer')scheduleSheetPreview();});
// Fields that depend on the imposition mode (e.g. booklet hides rows and columns) follow every change.
$('fields').addEventListener('change',()=>{if(!busy)conditionalFields();});$('fields').addEventListener('input',()=>{if(!busy)conditionalFields();});
const montaxBaseRenderTool=renderTool;
renderTool=function(key){montaxStrip.replaceChildren();montaxStrip.hidden=true;montaxBaseRenderTool(key);if(key!=='imposer')return;
// Montax panel order.
const order=montaxGroups.map(([name])=>name);const groups=[...$('fields').querySelectorAll(':scope>.field-group')];
groups.sort((a,b)=>order.indexOf(a.querySelector('summary').textContent)-order.indexOf(b.querySelector('summary').textContent)).forEach(group=>{group.open=true;$('fields').append(group);});
const margins=groups.find(group=>group.querySelector('summary').textContent==='Marginesy i odstępy impozycji');
if(margins){const title=document.createElement('strong');title.textContent='Marginesy i odstępy impozycji';const body=margins.querySelector('.group-fields');body.classList.add('strip-fields');montaxStrip.append(title,body);margins.remove();montaxStrip.hidden=false;}};
const montaxBaseControls=controls;
controls=function(){montaxBaseControls();montaxStrip.querySelectorAll('input,select').forEach(el=>el.disabled=busy);};
if(mode==='imposer')renderTool('imposer');
