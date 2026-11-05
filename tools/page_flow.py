"""Compact page-flow evidence for targeting visual-review captures."""
from __future__ import annotations


async def inspect_page_flow(page) -> dict:
    return await page.evaluate("""() => {
      const viewportHeight = window.innerHeight;
      const absRect = el => {
        const r = el.getBoundingClientRect();
        return {document_y: Math.max(0, Math.round(r.top + window.scrollY)),
          x: Math.round(r.left), width: Math.round(r.width), height: Math.round(r.height)};
      };
      const imageItems = [...document.querySelectorAll('img, video')].map(el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        const width = Math.round(r.width), height = Math.round(r.height);
        const loaded = el.tagName === 'IMG' ? el.complete && el.naturalWidth > 0 : el.videoWidth > 0;
        const inHeader = !!el.closest('header, nav, [role="banner"]');
        return {el, width, height, loaded, inHeader, visible: s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > .05};
      }).filter(x => x.visible && x.loaded && !x.inHeader && x.width >= 160 && x.height >= 100)
        .sort((a,b) => absRect(a.el).document_y - absRect(b.el).document_y);
      const images = imageItems.slice(0, 30).map(x => ({...absRect(x.el),
        type: x.el.tagName.toLowerCase(), alt: (x.el.getAttribute('alt') || x.el.getAttribute('aria-label') || '').slice(0,180),
        src: (x.el.currentSrc || x.el.getAttribute('src') || '').slice(0,500),
        viewport_transitions: Math.ceil(absRect(x.el).document_y / Math.max(1, viewportHeight))}));
      const forms = [...document.querySelectorAll('form,[role="form"]')].filter(el => {
        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
      }).map(el => ({el, ...absRect(el)})).sort((a,b) => a.document_y-b.document_y);
      const form = forms[0];
      const selects = form ? [...form.el.querySelectorAll('select')].map(el => {
        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        const fontSize=parseFloat(s.fontSize)||0, lineHeight=parseFloat(s.lineHeight)||fontSize*1.2;
        const available=Math.max(0, el.clientHeight-(parseFloat(s.paddingTop)||0)-(parseFloat(s.paddingBottom)||0));
        return {label: (el.labels?.[0]?.innerText || el.getAttribute('aria-label') || '').trim().slice(0,120),
          text: (el.options?.[el.selectedIndex]?.text || '').trim().slice(0,180),
          width:Math.round(r.width), height:Math.round(r.height), font_size_px:fontSize,
          line_height_px:lineHeight, text_box_height_px:available,
          possible_vertical_clipping: lineHeight > 0 && available > 0 && available < lineHeight};
      }) : [];
      const buttons = [...document.querySelectorAll('button,[role="button"],a.button,.button')].slice(0,80).map(el => ({
        text:(el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' ').slice(0,100),
        ...absRect(el)}));
      const h1=[...document.querySelectorAll('h1')].find(el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&r.top<viewportHeight&&r.bottom>0&&s.visibility!=='hidden';});
      const heroMedia=[];
      const addHero=(el,type,loaded=null)=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity||1)<=.05)return;
        const w=Math.min(innerWidth,r.right)-Math.max(0,r.left),h=Math.min(viewportHeight,r.bottom)-Math.max(0,r.top),area=w*h*100/Math.max(1,innerWidth*viewportHeight);
        if(w>=120&&h>=90&&area>=8&&loaded!==false)heroMedia.push({type,...absRect(el),viewport_area_percent:Math.round(area*10)/10,loaded});};
      for(const el of document.querySelectorAll('img,video')){const loaded=el.tagName==='IMG'?el.complete&&el.naturalWidth>0:el.videoWidth>0;addHero(el,el.tagName.toLowerCase(),loaded);}
      for(const el of [document.body,...document.querySelectorAll('body *')].slice(0,1600))if(/url\\s*\\(/i.test(getComputedStyle(el).backgroundImage||''))addHero(el,'background_image');
      return {viewport:{width:innerWidth,height:viewportHeight},
        meaningful_image_count:images.length, first_content_image:images[0]||null,
        content_images:images, first_contact_form:form ? {document_y:form.document_y,x:form.x,width:form.width,height:form.height,
          viewport_transitions:Math.ceil(form.document_y/Math.max(1,viewportHeight)), select_count:selects.length, selects} : null,
        hero_media:{status:!h1?'uncertain':heroMedia.length?'present':'absent',headline:h1?(h1.innerText||'').trim().slice(0,200):null,
          prominent_media_count:heroMedia.length,media_candidates:heroMedia.slice(0,20)},
        buttons, note:'Image and form positions are script-measured; possible select clipping is a heuristic and needs visual confirmation.'};
    }""")
