import { useQuery } from "@tanstack/react-query";

import { getSession } from "@/lib/api";

/** Who is asking: the signed-in user, and in demo mode the users the picker offers. Read once per page load. */
export function useSession() {
  return useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => getSession(signal),
    staleTime: Number.POSITIVE_INFINITY,
  });
}
