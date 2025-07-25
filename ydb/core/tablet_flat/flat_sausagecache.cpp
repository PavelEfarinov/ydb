#include "flat_sausagecache.h"
#include "util_fmt_abort.h"
#include <util/generic/xrange.h>

namespace NKikimr {
namespace NTabletFlatExecutor {

TPrivatePageCache::TPage::TPage(size_t size, TPageId pageId, TInfo* info)
    : LoadState(LoadStateNo)
    , Id(pageId)
    , Size(size)
    , Info(info)
{}

TPrivatePageCache::TInfo::TInfo(TIntrusiveConstPtr<NPageCollection::IPageCollection> pageCollection)
    : Id(pageCollection->Label())
    , PageCollection(std::move(pageCollection))
{
    PageMap.resize(PageCollection->Total());
}

TPrivatePageCache::TInfo::TInfo(const TInfo &info)
    : Id(info.Id)
    , PageCollection(info.PageCollection)
{
    PageMap.resize(info.PageMap.size());
    for (const auto& kv : info.PageMap) {
        auto* src = kv.second.Get();
        Y_DEBUG_ABORT_UNLESS(src);
        if (src->LoadState == TPage::LoadStateLoaded) {
            auto* dst = EnsurePage(src->Id);
            dst->LoadState = TPage::LoadStateLoaded;
            dst->SharedBody = src->SharedBody;
            dst->PinnedBody = src->PinnedBody;
        }
    }
}

TIntrusivePtr<TPrivatePageCache::TInfo> TPrivatePageCache::GetPageCollection(TLogoBlobID id) const {
    auto it = PageCollections.find(id);
    Y_ENSURE(it != PageCollections.end(), "trying to get unknown page collection. logic flaw?");
    return it->second;
}

void TPrivatePageCache::RegisterPageCollection(TIntrusivePtr<TInfo> info) {
    auto itpair = PageCollections.insert(decltype(PageCollections)::value_type(info->Id, info));
    Y_ENSURE(itpair.second, "double registration of page collection is forbidden. logic flaw?");
    ++Stats.TotalCollections;

    for (const auto& kv : info->PageMap) {
        auto* page = kv.second.Get();
        Y_ENSURE(page);
        Y_ENSURE(page->SharedBody, "New filled pages can't be without a shared body");

        Stats.TotalSharedBody += page->Size;
        if (page->PinnedBody)
            Stats.TotalPinnedBody += page->Size;

        TryUnload(page);
        // notify shared cache that we have a page handle
        ToTouchShared[page->Info->Id].insert(page->Id);
        Y_DEBUG_ABORT_UNLESS(!page->IsUnnecessary());
    }
}

void TPrivatePageCache::ForgetPageCollection(TIntrusivePtr<TInfo> info) {
    for (const auto& kv : info->PageMap) {
        auto* page = kv.second.Get();
        Y_DEBUG_ABORT_UNLESS(page);

        if (page->PinPad) {
            page->PinPad.Drop();
            Stats.PinnedSetSize -= page->Size;
            if (page->LoadState != TPage::LoadStateLoaded)
                Stats.PinnedLoadSize -= page->Size;
        }

        if (page->SharedBody)
            Stats.TotalSharedBody -= page->Size;
        if (page->PinnedBody)
            Stats.TotalPinnedBody -= page->Size;
        if (page->PinnedBody && !page->SharedBody)
            Stats.TotalExclusive -= page->Size;
    }

    info->PageMap.clear();
    PageCollections.erase(info->Id);
    ToTouchShared.erase(info->Id);
    --Stats.TotalCollections;
}

TPrivatePageCache::TInfo* TPrivatePageCache::Info(TLogoBlobID id) {
    auto *x = PageCollections.FindPtr(id);
    if (x)
        return x->Get();
    else
        return nullptr;
}

TIntrusivePtr<TPrivatePageCachePinPad> TPrivatePageCache::Pin(TPage *page) {
    Y_DEBUG_ABORT_UNLESS(page);
    if (page && !page->PinPad) {
        page->PinPad = new TPrivatePageCachePinPad();
        Stats.PinnedSetSize += page->Size;

        TryLoad(page);

        if (page->LoadState != TPage::LoadStateLoaded)
            Stats.PinnedLoadSize += page->Size;
    }

    return page->PinPad;
}

void TPrivatePageCache::Unpin(TPage *page, TPrivatePageCachePinPad *pad) {
    if (page && page->PinPad.Get() == pad) {
        if (page->PinPad.RefCount() == 1) {
            page->PinPad.Drop();
            Stats.PinnedSetSize -= page->Size;
            if (page->LoadState != TPage::LoadStateLoaded)
                Stats.PinnedLoadSize -= page->Size;

            TryUnload(page);
        }
    }
}

void TPrivatePageCache::TryLoad(TPage *page) {
    if (page->LoadState == TPage::LoadStateLoaded) {
        return;
    }

    if (page->LoadState == TPage::LoadStateNo && page->SharedBody) {
        if (page->SharedBody.Use()) {
            if (Y_LIKELY(!page->PinnedBody))
                Stats.TotalPinnedBody += page->Size;
            page->PinnedBody = TPinnedPageRef(page->SharedBody).GetData();
            page->LoadState = TPage::LoadStateLoaded;
            return;
        }

        page->SharedBody.Drop();
        Stats.TotalSharedBody -= page->Size;
        if (Y_UNLIKELY(page->PinnedBody)) {
            Stats.TotalExclusive += page->Size;
        }
    }
}

void TPrivatePageCache::TPrivatePageCache::TryUnload(TPage *page) {
    if (page->LoadState == TPage::LoadStateLoaded) {
        if (!page->PinPad) {
            ToTouchShared[page->Info->Id].insert(page->Id);
            page->LoadState = TPage::LoadStateNo;
            if (Y_LIKELY(page->PinnedBody)) {
                Stats.TotalPinnedBody -= page->Size;
                if (!page->SharedBody) {
                    Stats.TotalExclusive -= page->Size;
                }
                page->PinnedBody = { };
            }
            page->SharedBody.UnUse();
        }
    }
}

// page may be made free after this call
void TPrivatePageCache::TPrivatePageCache::TryEraseIfUnnecessary(TPage *page) {
    if (page->IsUnnecessary()) {
        if (Y_UNLIKELY(page->PinnedBody)) {
            Stats.TotalPinnedBody -= page->Size;
            Stats.TotalExclusive -= page->Size;
            page->PinnedBody = { };
        }
        const TPageId pageId = page->Id;
        auto* info = page->Info;
        Y_DEBUG_ABORT_UNLESS(info->PageMap[pageId].Get() == page);
        Y_ENSURE(info->PageMap.erase(pageId));
    }
}

const TSharedData* TPrivatePageCache::Lookup(TPageId pageId, TInfo *info) {
    TPage *page = info->EnsurePage(pageId);
    
    TryLoad(page);

    if (page->LoadState == TPage::LoadStateLoaded) {
        if (page->Empty()) {
            Touches.PushBack(page);
            Stats.CurrentCacheHits++;
            if (!page->IsSticky()) {
                Stats.CurrentCacheHitSize += page->Size;
            }
        }
        return &page->PinnedBody;
    }

    if (page->Empty()) {
        Y_DEBUG_ABORT_UNLESS(info->GetPageType(page->Id) != EPage::FlatIndex, "Flat index pages should have been sticked and preloaded");
        ToLoad.PushBack(page);
        Stats.CurrentCacheMisses++;
    }
    return nullptr;
}

void TPrivatePageCache::CountTouches(TPinned &pinned, ui32 &newPages, ui64 &newMemory, ui64 &pinnedMemory) {
    if (pinned.empty()) {
        newPages += Stats.CurrentCacheHits;
        newMemory += Stats.CurrentCacheHitSize;
        return;
    }

    for (auto &page : Touches) {
        bool isPinned = pinned[page.Info->Id].contains(page.Id);

        if (!isPinned) {
            newPages++;
        }

        // Note: it seems useless to count sticky pages in tx usage
        // also we want to read index from Env
        if (!page.IsSticky()) {
            if (isPinned) {
                pinnedMemory += page.Size;
            } else {
                newMemory += page.Size;
            }
        }
    }
}

void TPrivatePageCache::PinTouches(TPinned &pinned, ui32 &touchedPages, ui32 &pinnedPages, ui64 &pinnedMemory) {
    for (auto &page : Touches) {
        auto &pinnedCollection = pinned[page.Info->Id];
        
        // would insert only if first seen
        if (pinnedCollection.insert(std::make_pair(page.Id, Pin(&page))).second) {
            pinnedPages++;
            // Note: it seems useless to count sticky pages in tx usage
            // also we want to read index from Env
            if (!page.IsSticky()) {
                pinnedMemory += page.Size;
            }
        }
        touchedPages++;
    }
}

void TPrivatePageCache::PinToLoad(TPinned &pinned, ui32 &pinnedPages, ui64 &pinnedMemory) {
    for (auto &page : ToLoad) {
        auto &pinnedCollection = pinned[page.Info->Id];

        // would insert only if first seen
        if (pinnedCollection.insert(std::make_pair(page.Id, Pin(&page))).second) {
            pinnedPages++;
            // Note: it seems useless to count sticky pages in tx usage
            // also we want to read index from Env
            if (!page.IsSticky()) {
                pinnedMemory += page.Size;
            }
        }
    }
}

void TPrivatePageCache::UnpinPages(TPinned &pinned, size_t &unpinnedPages) {
    for (auto &xinfoid : pinned) {
        if (TPrivatePageCache::TInfo *info = Info(xinfoid.first)) {
            for (auto &x : xinfoid.second) {
                TPageId pageId = x.first;
                TPrivatePageCachePinPad *pad = x.second.Get();
                x.second.Reset();
                TPage *page = info->GetPage(pageId);
                Unpin(page, pad);
                unpinnedPages++;
            }
        }
    }
}

THashMap<TPrivatePageCache::TInfo*, TVector<TPageId>> TPrivatePageCache::GetToLoad() const {
    THashMap<TPrivatePageCache::TInfo*, TVector<TPageId>> result;
    for (auto &page : ToLoad) {
        result[page.Info].push_back(page.Id);
    }
    return result;
}

void TPrivatePageCache::ResetTouchesAndToLoad(bool verifyEmpty) {
    if (verifyEmpty) {
        Y_ENSURE(!Touches);
        Y_ENSURE(!Stats.CurrentCacheHits);
        Y_ENSURE(!Stats.CurrentCacheHitSize);
        Y_ENSURE(!ToLoad);
        Y_ENSURE(!Stats.CurrentCacheMisses);
    }

    while (Touches) {
        TPage *page = Touches.PopBack();
        TryUnload(page);
    }
    Stats.CurrentCacheHits = 0;
    Stats.CurrentCacheHitSize = 0;

    while (ToLoad) {
        TPage *page = ToLoad.PopBack();
        TryEraseIfUnnecessary(page);
    }
    Stats.CurrentCacheMisses = 0;
}

void TPrivatePageCache::DropSharedBody(TInfo *info, TPageId pageId) {
    TPage *page = info->GetPage(pageId);
    if (!page)
        return;

    if (!page->SharedBody.IsUsed()) {
        if (Y_LIKELY(page->SharedBody)) {
            Stats.TotalSharedBody -= page->Size;
            if (Y_UNLIKELY(page->PinnedBody)) {
                Stats.TotalExclusive += page->Size;
            }
            page->SharedBody = { };
        }
        TryEraseIfUnnecessary(page);
    }
}

void TPrivatePageCache::ProvideBlock(
        NSharedCache::TEvResult::TLoaded&& loaded, TInfo *info)
{
    Y_DEBUG_ABORT_UNLESS(loaded.Page && loaded.Page.IsUsed());
    TPage *page = info->EnsurePage(loaded.PageId);

    if (page->LoadState != TPage::LoadStateLoaded && page->PinPad)
        Stats.PinnedLoadSize -= page->Size;

    if (Y_UNLIKELY(page->SharedBody))
        Stats.TotalSharedBody -= page->Size;
    if (Y_UNLIKELY(page->PinnedBody))
        Stats.TotalPinnedBody -= page->Size;
    if (Y_UNLIKELY(page->PinnedBody && !page->SharedBody))
        Stats.TotalExclusive -= page->Size;

    page->Fill(std::move(loaded.Page));
    Stats.TotalSharedBody += page->Size;
    Stats.TotalPinnedBody += page->Size;
    TryUnload(page);
}

THashMap<TLogoBlobID, TIntrusivePtr<TPrivatePageCache::TInfo>> TPrivatePageCache::DetachPrivatePageCache() {
    THashMap<TLogoBlobID, TIntrusivePtr<TPrivatePageCache::TInfo>> ret;

    for (const auto &xpair : PageCollections) {
        TIntrusivePtr<TInfo> info(new TInfo(*xpair.second));
        ret.insert(std::make_pair(xpair.first, info));
    }

    return ret;
}

THashMap<TLogoBlobID, THashSet<TPrivatePageCache::TPageId>> TPrivatePageCache::GetPrepareSharedTouched() {
    return std::move(ToTouchShared);
}

}}
